"""
app/utils/redis_cache.py
─────────────────────────
Lightweight Redis read-through cache shared by orchestrator workers.

Uses DB 2 of the same Redis instance as the Celery broker so no extra
connection URL is needed.  DB 0 is the Celery broker queue and DB 1 is
used by Celery as the result backend (configurable), so DB 2 is free.

Design:
  • Singleton (per-process) — one connection pool reused across all tasks.
  • All keys are namespaced under "vtcache:" to avoid collision.
  • get() / set() work with JSON-serialisable values only.
  • Ping() is used by the readiness probe.
  • If Redis is unavailable the cache degrades gracefully (returns None on
    get, silently skips set) so the service continues to function.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CACHE_DB = 2
_KEY_PREFIX = "vtcache:"

_cache_instance: Optional["RedisCache"] = None
_cache_lock = threading.Lock()


class RedisCache:
    """Thin wrapper around a Redis client for TTL-based JSON caching."""

    def __init__(self, broker_url: str) -> None:
        import redis
        # Re-use the broker URL but swap to DB 2 for cache isolation.
        self._client = redis.from_url(
            broker_url,
            db=_CACHE_DB,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value, or None on miss / error."""
        try:
            raw = self._client.get(_KEY_PREFIX + key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.debug("redis_cache: get failed key=%s err=%s", key, exc)
            return None

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """Cache *value* under *key* for *ttl* seconds.  Silently ignores errors."""
        try:
            self._client.setex(_KEY_PREFIX + key, ttl, json.dumps(value))
        except Exception as exc:
            logger.debug("redis_cache: set failed key=%s err=%s", key, exc)

    def delete(self, key: str) -> None:
        """Delete a specific cache key.  Silently ignores errors."""
        try:
            self._client.delete(_KEY_PREFIX + key)
        except Exception as exc:
            logger.debug("redis_cache: delete failed key=%s err=%s", key, exc)

    def delete_pattern(self, pattern: str) -> None:
        """
        Delete all keys matching *pattern* (e.g. ``"rule:tenant1:*"``).
        Uses SCAN to avoid blocking Redis with KEYS on large keyspaces.
        """
        try:
            full_pattern = _KEY_PREFIX + pattern
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor, match=full_pattern, count=100)
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as exc:
            logger.debug("redis_cache: delete_pattern failed pattern=%s err=%s", pattern, exc)

    def ping(self) -> bool:
        """Return True if Redis is reachable."""
        try:
            self._client.ping()
            return True
        except Exception:
            return False


# ── Singleton accessor ────────────────────────────────────────────────────────

def get_cache() -> Optional[RedisCache]:
    """
    Return the process-level RedisCache singleton, or None if Redis is
    not configured (no CELERY_BROKER_URL pointing at Redis).
    """
    global _cache_instance
    if _cache_instance is None:
        with _cache_lock:
            if _cache_instance is None:
                try:
                    from app.config import settings
                    broker = settings.CELERY_BROKER_URL
                    if not broker.startswith("redis"):
                        return None
                    _cache_instance = RedisCache(broker)
                    logger.debug("redis_cache: initialised db=%d", _CACHE_DB)
                except Exception as exc:
                    logger.warning("redis_cache: init failed — caching disabled: %s", exc)
                    return None
    return _cache_instance
