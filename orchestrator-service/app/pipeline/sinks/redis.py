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
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_SINK_KIND  = "redis"
_META_KEY   = "__vartrack__"


class RedisSink(BaseSink):
    object_name = "JSON value"

    def __init__(
        self,
        *,
        name: str = "redis",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            import redis as redislib
        except ImportError as exc:
            raise RuntimeError("redis-py is required: pip install redis") from exc

        cfg   = rule_config or {}
        hosts = cfg.get("redis_hosts", ["localhost:6379"])
        password = cfg.get("redis_password")

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
                db=int(cfg.get("redis_db", 0)),
                decode_responses=True,
            )
        self._tenant_id      = tenant_id
        self._prefix         = cfg.get("redis_key_prefix", "")
        # destination_template: prefix template, e.g. "{tenant}:{env}".
        # Takes precedence over redis_key_prefix when set.
        self._dest_tpl = cfg.get("destination_template", "")
        self._structure      = cfg.get("redis_data_structure", "HASH").upper()
        self._hash_key  = cfg.get("redis_hash_key", "vartrack")
        self._ttl       = cfg.get("redis_ttl_seconds")

        # HASH → DOT,  STRING → FLAT.  Caller can override via key_format.
        default_fmt = "dot" if self._structure == "HASH" else "flat"
        fmt_name    = cfg.get("key_format", default_fmt)
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

        if self._structure == "HASH":
            written = self._write_hash(formatted, prefix)
        elif self._structure == "JSON":
            written = self._write_json(flat_data, prefix)
        else:
            written = self._write_strings(formatted, prefix)

        # Stamp meta hash with VarTrack labels
        self._redis.hset(f"{prefix}:{_META_KEY}", mapping=labels.as_redis_meta())

        return {"written": written}

    def _write_hash(self, data: dict[str, str], prefix: str) -> int:
        if data:
            self._redis.hset(f"{prefix}:{self._hash_key}", mapping=data)
        return len(data)

    def _write_json(self, data: dict[str, str], prefix: str) -> int:
        key = f"{prefix}:json"
        self._redis.set(key, json.dumps(data), ex=self._ttl if self._ttl else None)
        return 1

    def _write_strings(self, data: dict[str, str], prefix: str) -> int:
        pipe = self._redis.pipeline(transaction=False)
        for k, v in data.items():
            rk = f"{prefix}:{k}"
            pipe.set(rk, v)
            if self._ttl:
                pipe.expire(rk, self._ttl)
        pipe.execute()
        return len(data)

    @classmethod
    def prunes_keys(cls, rule_config: dict) -> bool:
        # The JSON structure is one string holding everything: written whole.
        return str(rule_config.get("redis_data_structure", "HASH")).upper() != "JSON"

    @classmethod
    def key_space(cls, rule_config: dict, tenant_id: str, env: str) -> str:
        prefix = cls._prefix_of(rule_config, tenant_id, rule_config.get("datasource", ""), env)
        if str(rule_config.get("redis_data_structure", "HASH")).upper() == "HASH":
            return f"{prefix}:{rule_config.get('redis_hash_key', 'vartrack')}"
        return prefix

    @classmethod
    def store(cls, rule_config: dict) -> str:
        return cls._store_of(rule_config.get("redis_hosts"), rule_config.get("redis_db"))

    def delete_keys(self, keys: "Collection[str]", *, datasource: str, env: str) -> int:
        if not keys:
            return 0
        prefix = self._key_prefix(datasource, env)
        formatted = list(format_keys(dict.fromkeys(keys, ""), self._key_format))
        if self._structure == "HASH":
            return int(self._redis.hdel(f"{prefix}:{self._hash_key}", *formatted))
        return int(self._redis.delete(*[f"{prefix}:{k}" for k in formatted]))

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:
            pass

    @staticmethod
    def _prefix_of(rule_config: dict, tenant_id: str, datasource: str, env: str) -> str:
        # destination_template takes precedence over redis_key_prefix.
        tpl = rule_config.get("destination_template", "")
        if tpl:
            return resolve_destination(tpl, tenant=tenant_id, env=env)
        return rule_config.get("redis_key_prefix", "") or f"{tenant_id}:{datasource}:{env}"

    def _key_prefix(self, datasource: str, env: str) -> str:
        """Compute the Redis key prefix for a given datasource/env."""
        return self._prefix_of(
            {"destination_template": self._dest_tpl, "redis_key_prefix": self._prefix},
            self._tenant_id, datasource, env,
        )

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
