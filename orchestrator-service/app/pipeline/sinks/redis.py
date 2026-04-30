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
        prune: bool,
        prune_last: bool,
        prune_protection: list[str],
        dry_run_prune: bool,
        total_sources: int,
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

        pruned = 0
        if prune and not dry_run_prune:
            pruned = self._prune(flat_data, prefix, prune_protection, prune_last)

        return {"written": written, "pruned": pruned}

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

    def _prune(
        self, flat_data: dict[str, str], prefix: str,
        protection: list[str], last: bool,
    ) -> int:
        import fnmatch
        from app.pipeline.sinks.key_formatter import format_keys
        formatted_keys = set(format_keys(flat_data, self._key_format).keys())

        if self._structure == "HASH":
            hk        = f"{prefix}:{self._hash_key}"
            live      = set(self._redis.hkeys(hk))
            to_delete = [
                k for k in live
                if k not in formatted_keys
                and not any(fnmatch.fnmatch(k, p) for p in protection)
            ]
            if last:
                to_delete = to_delete[-1:]
            if to_delete:
                self._redis.hdel(hk, *to_delete)
            return len(to_delete)

        live = set(self._redis.scan_iter(f"{prefix}:*"))
        to_delete = [
            rk for rk in live
            if not rk.endswith(_META_KEY)
            and rk[len(prefix) + 1:] not in formatted_keys
            and not any(fnmatch.fnmatch(rk[len(prefix) + 1:], p) for p in protection)
        ]
        if last:
            to_delete = to_delete[-1:]
        if to_delete:
            self._redis.delete(*to_delete)
        return len(to_delete)

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:
            pass

    def _key_prefix(self, datasource: str, env: str) -> str:
        """Compute the Redis key prefix for a given datasource/env."""
        if self._dest_tpl:
            return resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=env)
        return self._prefix or f"{self._tenant_id}:{datasource}:{env}"

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
            logger.warning("RedisSink.read_values failed: %s", exc)
            return {}
