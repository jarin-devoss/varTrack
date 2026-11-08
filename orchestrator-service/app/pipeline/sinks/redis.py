"""
app/pipeline/sinks/redis.py
────────────────────────────
Redis sink.

Key format: DOT (default, HASH mode) or FLAT (STRING mode)
──────────────────────────────────────────────────────────
  HASH   mode → DOT   "db.host" stored as hash field inside one Redis hash key
  STRING mode → FLAT  "db.host" → key "{prefix}:db_host" as a Redis string

SLASH and SLASH_DOT are not supported.
rule.proto SinkKeyFormat.redis enforces this at validation time.

Env namespace
─────────────
The env is baked into the Redis key prefix so every env is isolated:
    {tenant_id}:{datasource}:{env}:…

For "pr-42":   acme:payments:pr-42:db_host
For "main":    acme:payments:main:db_host

No explicit provisioning is needed — Redis keys are created on first write.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Collection
from typing import Any, TYPE_CHECKING

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.utils.enums.sync_mode import SyncMode

if TYPE_CHECKING:
    from app.pipeline.sinks.settings import Settings

logger = logging.getLogger(__name__)

_SINK_KIND  = "redis"
_META_KEY   = "__vartrack__"


class RedisSink(BaseSink):
    object_name = "JSON value"

    def __init__(
        self,
        *,
        name: str = "redis",
        settings: "Settings | None" = None,
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            import redis as redislib
        except ImportError as exc:
            raise RuntimeError("redis-py is required: pip install redis") from exc

        from app.pipeline.sinks.settings import Settings

        s     = settings or Settings.of("redis")
        rule  = rule_config or {}
        hosts = s.get("hosts") or ["localhost:6379"]
        password = s.secret("password")

        if len(hosts) > 1:
            startup_nodes = []
            for h in hosts:
                host, _, port = h.partition(":")
                startup_nodes.append(
                    redislib.cluster.ClusterNode(host, int(port or 6379))
                )
            self._redis = redislib.RedisCluster(
                startup_nodes=startup_nodes,
                password=password,
                decode_responses=True,
                skip_full_coverage_check=True,  # don't require all slots covered
            )
        else:
            host, _, port = hosts[0].partition(":")
            self._redis = redislib.Redis(
                host=host, port=int(port or 6379),
                password=password,
                db=int(s.get("database", 0)),
                decode_responses=True,
                # without these the client waits for ever on a server that hangs; None keeps that
                socket_connect_timeout=float(s.seconds("connection_timeout") or 0) or None,
                socket_timeout=float(s.seconds("read_timeout") or 0) or None,
            )
        self._clustered      = len(hosts) > 1
        self._tenant_id      = tenant_id
        self._prefix         = s.get("key_prefix", "")
        # destination_template: prefix template, e.g. "{tenant}:{env}".
        # Takes precedence over key_prefix when set.
        self._dest_tpl = rule.get("destination_template", "")
        self._structure      = self.structure_of(s)
        self._hash_key  = s.get("hash_key", "vartrack")
        self._ttl       = s.seconds("ttl")

        # HASH → DOT,  STRING → FLAT.  Caller can override via key_format.
        default_fmt = "dot" if self._structure == "HASH" else "flat"
        fmt_name    = rule.get("key_format", default_fmt)
        self._key_format = key_format_from_str(fmt_name)
        validate_sink_format(_SINK_KIND, self._key_format)

        logger.debug(
            "RedisSink ready structure=%s fmt=%s",
            self._structure, self._key_format.value,
        )

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
        prefix = self._key_prefix(datasource, env)
        formatted = format_keys(flat_data, self._key_format)

        # The data, its expiry and the labels that stamp it go to Redis as one MULTI/EXEC: they all happen
        # or (when the connection dies before EXEC) none does, never the data without the stamp.
        pipe = self._redis.pipeline(transaction=not self._clustered)
        if self._structure == "HASH":
            written = self._queue_hash(pipe, formatted, prefix)
        elif self._structure == "JSON":
            written = self._queue_json(pipe, flat_data, prefix)
        else:
            written = self._queue_strings(pipe, formatted, prefix)
        pipe.hset(f"{prefix}:{_META_KEY}", mapping=labels.as_redis_meta())
        self._execute(pipe)

        return {"written": written}

    def _queue_hash(self, pipe: Any, data: dict[str, str], prefix: str) -> int:
        if data:
            pipe.hset(f"{prefix}:{self._hash_key}", mapping=data)
        return len(data)

    def _queue_json(self, pipe: Any, data: dict[str, str], prefix: str) -> int:
        pipe.set(f"{prefix}:json", json.dumps(data), ex=self._ttl if self._ttl else None)
        return 1

    def _queue_strings(self, pipe: Any, data: dict[str, str], prefix: str) -> int:
        for k, v in data.items():
            rk = f"{prefix}:{k}"
            pipe.set(rk, v)
            if self._ttl:
                pipe.expire(rk, self._ttl)
        return len(data)

    def _execute(self, pipe: Any) -> None:
        """Run the queued commands and look at every reply: a command Redis refused (the key holds another
        type, a script flag, memory) is an error of the write, not something to find out later.

        EXEC is not a rollback: Redis runs the commands that are accepted and reports the ones that are not,
        so a refusal here can leave the others applied. The sync then fails and a retry writes the same
        values again, which is idempotent. A Redis Cluster cannot run one MULTI over keys in different hash
        slots (the data and the stamp are two keys), so there the same commands are one pipeline instead."""
        replies = pipe.execute(raise_on_error=False)
        failed = [r for r in replies if isinstance(r, Exception)]
        if failed:
            raise RuntimeError(
                f"Redis refused {len(failed)} of {len(replies)} commands of the write: "
                f"{'; '.join(str(f) for f in failed[:3])}"
            )

    @staticmethod
    def structure_of(settings: Any) -> str:
        """HASH (the default), STRING or JSON."""
        return str(settings.get("data_structure") or "HASH").upper()

    @classmethod
    def prunes_keys(cls, settings: Any, rule_config: dict) -> bool:
        # The JSON structure is one string holding everything: written whole.
        return cls.structure_of(settings) != "JSON"

    @classmethod
    def key_space(cls, settings: Any, rule_config: dict, tenant_id: str, env: str) -> str:
        datasource = rule_config.get("datasource") or settings.name
        prefix = cls._prefix_of(rule_config.get("destination_template", ""), settings.get("key_prefix", ""),
                                tenant_id, datasource, env)
        if cls.structure_of(settings) == "HASH":
            return f"{prefix}:{settings.get('hash_key', 'vartrack')}"
        return prefix

    @classmethod
    def store(cls, settings: Any, rule_config: dict) -> str:
        return cls._store_of(settings.get("hosts"), settings.get("database"))

    def delete_keys(self, keys: "Collection[str]", *, datasource: str, env: str) -> int:
        if not keys:
            return 0
        prefix = self._key_prefix(datasource, env)
        formatted = list(format_keys(dict.fromkeys(keys, ""), self._key_format))
        # one HDEL / DEL: Redis runs a single command whole, so the prune is already one step
        if self._structure == "HASH":
            return int(self._redis.hdel(f"{prefix}:{self._hash_key}", *formatted))
        return int(self._redis.delete(*[f"{prefix}:{k}" for k in formatted]))

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:
            pass

    @staticmethod
    def _prefix_of(template: str, key_prefix: str, tenant_id: str, datasource: str, env: str) -> str:
        # destination_template (the rule's) takes precedence over key_prefix (the datasource's).
        if template:
            return resolve_destination(template, tenant=tenant_id, env=env)
        return key_prefix or f"{tenant_id}:{datasource}:{env}"

    def _key_prefix(self, datasource: str, env: str) -> str:
        """Compute the Redis key prefix for a given datasource/env."""
        return self._prefix_of(self._dest_tpl, self._prefix, self._tenant_id, datasource, env)

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """Read current values for a set of field keys from Redis."""
        if not keys:
            return {}
        prefix = self._key_prefix(datasource, env)
        try:
            key_list = list(keys)
            formatted = format_keys(dict.fromkeys(key_list, ""), self._key_format)
            fmt_keys = list(formatted.keys())
            if self._structure == "HASH":
                vals = self._redis.hmget(f"{prefix}:{self._hash_key}", fmt_keys)
                return {orig: v for orig, v in zip(key_list, vals) if v is not None}
            else:
                rkeys = [f"{prefix}:{fk}" for fk in fmt_keys]
                vals = self._redis.mget(rkeys)
                return {orig: v for orig, v in zip(key_list, vals) if v is not None}
        except Exception as exc:
            return self._read_failed(exc, "RedisSink.read_values")
