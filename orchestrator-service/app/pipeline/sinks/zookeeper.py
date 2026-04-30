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
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    KeyFormat, format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_SINK_KIND      = "zookeeper"
_DEFAULT_FORMAT = KeyFormat.SLASH
_META_ZNODE     = "__vartrack__"


class ZookeeperSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "zookeeper",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            from kazoo.client import KazooClient
        except ImportError as exc:
            raise RuntimeError("kazoo is required: pip install kazoo") from exc

        cfg = rule_config or {}

        self._base_path     = cfg.get("zk_base_path", "").rstrip("/")
        self._tenant_id     = tenant_id
        # destination_template is the canonical path template ({tenant}, {env} placeholders).
        # zk_path_template is kept as a ZK-specific fallback for backwards compat.
        self._path_template = (
            cfg.get("destination_template")
            or cfg.get("zk_path_template")
            or "/{tenant}/{env}"
        )
        hosts           = cfg.get("zk_hosts", ["localhost:2181"])
        timeout         = int(cfg.get("zk_timeout_secs", 10))

        fmt_name         = cfg.get("key_format", "slash")
        self._key_format = key_format_from_str(fmt_name)
        validate_sink_format(_SINK_KIND, self._key_format)

        self._zk = KazooClient(hosts=",".join(hosts), timeout=timeout)
        user = cfg.get("zk_username")
        pwd  = cfg.get("zk_password")
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
        prune: bool,
        prune_last: bool,
        prune_protection: list[str],
        dry_run_prune: bool,
        total_sources: int,
    ) -> dict[str, Any]:
        # Auto-provision: ensure_path creates every node in the path that
        # doesn't exist yet, including new env subtrees like /pr-42/.
        root    = self._root_path(env)
        self._zk.ensure_path(root)

        # Stamp __vartrack__ meta znode for this env
        meta_path = f"{root}/{_META_ZNODE}"
        self._zk.ensure_path(meta_path)
        self._zk.set(meta_path, labels.as_zk_meta())

        # Convert dot keys → znode paths
        # "db.host" → "/db/host"   (SLASH)
        # "db.host" → ".db.host"   (SLASH_DOT)
        formatted = format_keys(flat_data, self._key_format)

        from kazoo.exceptions import NoNodeError, NodeExistsError

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

        written = 0
        for key, value in to_write.items():
            relative = rel_paths[key]
            path     = f"{root}/{relative}"
            encoded  = value.encode("utf-8")
            try:
                self._zk.set(path, encoded)
            except NoNodeError:
                try:
                    self._zk.create(path, encoded, makepath=True)
                except NodeExistsError:
                    self._zk.set(path, encoded)
            written += 1

        pruned = 0
        if prune and not dry_run_prune:
            pruned = self._prune(flat_data, root, prune_protection, prune_last)

        return {"written": written, "pruned": pruned}

    def _prune(
        self, flat_data: dict[str, str], root: str,
        protection: list[str], last: bool,
    ) -> int:
        import fnmatch
        from app.pipeline.sinks.key_formatter import format_keys
        if not self._zk.exists(root):
            return 0

        # Build live top-level segment set from formatted keys.
        # SLASH:     "db.host" → "/db/host" → first segment after strip = "db"
        # SLASH_DOT: "db.host" → ".db.host" → first segment after strip = "db"
        formatted = format_keys(flat_data, self._key_format)
        live_top: set[str] = set()
        for fkey in formatted:
            clean = fkey.lstrip("/.")
            first = clean.split("/")[0] if "/" in clean else clean.split(".")[0]
            if first:
                live_top.add(first)

        to_delete = []
        for child in self._zk.get_children(root):
            if child == _META_ZNODE:
                continue
            if child in live_top:
                continue
            if any(fnmatch.fnmatch(child, p) for p in protection):
                continue
            to_delete.append(child)

        if last:
            to_delete = to_delete[-1:]
        for child in to_delete:
            self._zk.delete(f"{root}/{child}", recursive=True)
        return len(to_delete)

    def close(self) -> None:
        try:
            self._zk.stop()
            self._zk.close()
        except Exception:
            pass

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
                logger.warning("ZookeeperSink.read_values path=%s failed: %s", path, exc)
        return result
