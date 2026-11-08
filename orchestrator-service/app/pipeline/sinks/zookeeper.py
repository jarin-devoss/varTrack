"""
app/pipeline/sinks/zookeeper.py
─────────────────────────────────
ZooKeeper sink.

Key format: SLASH (default) or SLASH_DOT
──────────────────────────────────────────
ZooKeeper stores values at hierarchical znode paths.

  "slash"      (default)  "db.host.port" → /acme/pr-42/db/host/port
  "slash_dot"             "db.host.port" → .db.host.port   (some legacy setups)

DOT and FLAT are not valid for ZooKeeper.
rule.proto SinkKeyFormat.zookeeper enforces this at validation time.

update_strategy
───────────────
A znode per key is the only shape ZooKeeper stores, so STRATEGY_KEY_VALUE
(or unset) is the only update_strategy; DOCUMENT and FILE are refused.

Auto-provision
──────────────
When an env is new (e.g. "pr-42" from a first PR push), the sink creates
the full znode path automatically via ensure_path() before writing.

zk_path_template: free-form path with {tenant} and {env} placeholders (default "/{tenant}/{env}"):
    "/{tenant}/{env}"             → /acme/main/db/host
    "/{env}/{tenant}"             → /main/acme/db/host
    "/dc1/{env}/cfg/{tenant}"     → /dc1/main/cfg/acme/db/host
    zk_base_path prepended if set: "/ns" + "/{tenant}/{env}" → /ns/acme/main/db/host

So "pr-42", "v1.2.3", "feature-login" all get isolated subtrees without
any manual znode creation.

VarTrack labels are written as JSON to a __vartrack__ znode alongside the
config keys:
    /acme/pr-42/__vartrack__
        ← {"app.kubernetes.io/managed-by": "vartrack", "vartrack.io/env": "pr-42", ...}
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any, TYPE_CHECKING

from app.pipeline.sinks.base import BaseSink, update_strategy
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    KeyFormat, format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.utils.enums.sync_mode import SyncMode

if TYPE_CHECKING:
    from app.pipeline.sinks.settings import Settings

logger = logging.getLogger(__name__)

_SINK_KIND      = "zookeeper"
_DEFAULT_FORMAT = KeyFormat.SLASH
_META_ZNODE     = "__vartrack__"

# A transaction is one request, and a server refuses one over jute.maxbuffer (1 MiB by default). A file whose
# znodes do not fit is written as several transactions, each of them whole.
_TXN_BUDGET     = 800_000
_TXN_OP_COST    = 64              # a znode op's overhead on top of its path and data


def _batches(znodes: dict[str, bytes], meta_path: str, meta: bytes) -> list[dict[str, bytes]]:
    """The znodes split into transactions that each fit a request. The stamp goes in the last one, so it
    is only written once every value it describes is."""
    batches: list[dict[str, bytes]] = []
    current: dict[str, bytes] = {}
    size = len(meta_path) + len(meta) + _TXN_OP_COST      # reserved for the stamp
    for path, value in znodes.items():
        cost = len(path) + len(value) + _TXN_OP_COST
        if current and size + cost > _TXN_BUDGET:
            batches.append(current)
            current, size = {}, len(meta_path) + len(meta) + _TXN_OP_COST
        current[path] = value
        size += cost
    current[meta_path] = meta
    batches.append(current)
    return batches


class ZookeeperSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "zookeeper",
        settings: "Settings | None" = None,
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            from kazoo.client import KazooClient
        except ImportError as exc:
            raise RuntimeError("kazoo is required: pip install kazoo") from exc

        from app.pipeline.sinks.settings import Settings

        s    = settings or Settings.of("zookeeper")
        rule = rule_config or {}

        strategy = update_strategy(s.get("update_strategy"))
        if strategy not in ("STRATEGY_UNSPECIFIED", "STRATEGY_KEY_VALUE"):
            raise ValueError(
                f"ZooKeeper stores one znode per key; update_strategy {strategy} is not supported. "
                "Use STRATEGY_KEY_VALUE or leave it unset."
            )

        self._base_path     = s.get("base_path", "").rstrip("/")
        self._tenant_id     = tenant_id
        # destination_template is the path template ({tenant}, {env} placeholders). Its former
        # name, zk_path_template, is renamed to it when the rule is built.
        self._path_template = rule.get("destination_template") or "/{tenant}/{env}"
        hosts           = s.get("hosts") or ["localhost:2181"]
        timeout         = s.seconds("session_timeout") or 10

        fmt_name         = rule.get("key_format", "slash")
        self._key_format = key_format_from_str(fmt_name)
        validate_sink_format(_SINK_KIND, self._key_format)

        self._zk = KazooClient(hosts=",".join(hosts), timeout=timeout)
        user = s.get("username")
        pwd  = s.secret("password")
        if user and pwd:
            self._zk.add_auth("digest", f"{user}:{pwd}")
        self._zk.start(timeout=timeout)

        logger.debug("ZookeeperSink ready base=%s fmt=%s", self._base_path, self._key_format.value)

    # ── Write ─────────────────────────────────────────────────────────────────

    def _write(
        self,
        *,
        flat_data: dict[str, str],
        sync_mode: SyncMode,
        datasource: str,
        env: str,
        repo: str,
        branch: str,
        commit_sha: str,
        file_path: str,
        labels: VarTrackLabels,
        total_sources: int,
        data: Any = None,
    ) -> dict[str, Any]:
        # Auto-provision: ensure_path creates every node in the path that
        # doesn't exist yet, including new env subtrees like /pr-42/.
        root    = self._root_path(env)
        self._zk.ensure_path(root)

        meta_path = f"{root}/{_META_ZNODE}"
        self._zk.ensure_path(meta_path)

        # Convert dot keys → znode paths
        # "db.host" → "/db/host"   (SLASH)
        # "db.host" → ".db.host"   (SLASH_DOT)
        formatted = format_keys(flat_data, self._key_format)

        # Build a map of key → relative path for reuse below
        rel_paths = {
            key: key.lstrip("/").lstrip(".")
            for key in formatted
        }

        try:
            from vartrack_core import py_diff  # type: ignore[import]
            current: dict[str, str] = {}
            for key, relative in rel_paths.items():
                path = f"{root}/{relative}"
                if self._zk.exists(path):
                    data, _ = self._zk.get(path)
                    current[key] = (data or b"").decode("utf-8")
            diff = py_diff(current, formatted)
            to_write = {**diff["added"], **{k: v["new"] for k, v in diff["changed"].items()}}
        except ImportError:
            to_write = formatted

        znodes = {f"{root}/{rel_paths[key]}": value.encode("utf-8") for key, value in to_write.items()}
        for parent in sorted({path.rsplit("/", 1)[0] for path in znodes} - {root}):
            self._zk.ensure_path(parent)      # idempotent, and a transaction cannot make a path

        # The values and the stamp that says who wrote them are one transaction: ZooKeeper applies all of
        # its operations or none, so a failed write leaves the znodes as they were, stamp included.
        self._commit(znodes, meta_path, labels.as_zk_meta())
        return {"written": len(znodes)}

    def _commit(self, znodes: dict[str, bytes], meta_path: str, meta: bytes) -> None:
        batches = _batches(znodes, meta_path, meta)
        if len(batches) > 1:
            logger.warning(
                "ZookeeperSink: %d znodes do not fit one transaction (%d bytes); writing %d transactions, "
                "each whole, so a failure between them leaves the earlier ones written",
                len(znodes), sum(len(p) + len(v) for p, v in znodes.items()), len(batches),
            )
        for batch in batches:
            self._commit_batch(batch)

    def _commit_batch(self, batch: dict[str, bytes]) -> None:
        from kazoo.exceptions import NoNodeError, NodeExistsError, RolledBackError

        failure: Exception | None = None
        # A node that appeared (or went) between looking and the transaction fails it: look again once.
        for _ in range(2):
            txn = self._zk.transaction()
            for path, value in batch.items():
                if self._zk.exists(path):
                    txn.set_data(path, value)
                else:
                    txn.create(path, value)
            results = txn.commit()
            errors = [r for r in results if isinstance(r, Exception)]
            if not errors:
                return
            # the failing op carries its own error; the others report RolledBackError
            failure = next((e for e in errors if not isinstance(e, RolledBackError)), errors[0])
            if not isinstance(failure, (NoNodeError, NodeExistsError)):
                break
        raise RuntimeError(
            f"ZooKeeper refused the transaction of {len(batch)} znodes ({type(failure).__name__}: {failure}); "
            f"none of them was written"
        )

    @classmethod
    def prunes_keys(cls, settings: Any, rule_config: dict) -> bool:
        return True

    @classmethod
    def key_space(cls, settings: Any, rule_config: dict, tenant_id: str, env: str) -> str:
        return cls._root_of(settings, rule_config, tenant_id, env)

    @classmethod
    def store(cls, settings: Any, rule_config: dict) -> str:
        return cls._store_of(settings.get("hosts"))

    def delete_keys(self, keys: "Collection[str]", *, datasource: str, env: str) -> int:
        # Only the znodes of the keys themselves — never the subtrees around
        # them, which may hold nodes other tools put there — and only leaves.
        root = self._root_path(env)
        deleted = 0
        for key in sorted(keys):
            relative = next(iter(format_keys({key: ""}, self._key_format))).lstrip("/").lstrip(".")
            path = f"{root}/{relative}"
            if self._zk.exists(path) and not self._zk.get_children(path):
                self._zk.delete(path)
                deleted += 1
        return deleted

    def close(self) -> None:
        try:
            self._zk.stop()
            self._zk.close()
        except Exception:
            pass

    @staticmethod
    def _root_of(settings: Any, rule_config: dict, tenant_id: str, env: str) -> str:
        # destination_template (the rule's) is the path template ({tenant}, {env} placeholders),
        # under the datasource's base_path.
        template = rule_config.get("destination_template") or "/{tenant}/{env}"
        base = settings.get("base_path", "").rstrip("/")
        return (base + resolve_destination(template, tenant=tenant_id, env=env)).rstrip("/")

    def _root_path(self, env: str) -> str:
        """Compute the ZooKeeper root path for a given env."""
        subtree = resolve_destination(self._path_template, tenant=self._tenant_id, env=env)
        return (self._base_path + subtree).rstrip("/")

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """Read current values for a set of field keys from ZooKeeper."""
        if not keys:
            return {}
        root = self._root_path(env)
        result: dict[str, str] = {}
        formatted = format_keys(dict.fromkeys(keys, ""), self._key_format)
        for flat_key, zk_key in zip(keys, formatted.keys()):
            path = f"{root}/{zk_key}"
            try:
                if self._zk.exists(path):
                    data, _ = self._zk.get(path)
                    result[flat_key] = (data or b"").decode("utf-8", errors="replace")
            except Exception as exc:
                if self.strict_reads:
                    raise
                logger.warning("ZookeeperSink.read_values path=%s failed: %s", path, exc)
        return result
