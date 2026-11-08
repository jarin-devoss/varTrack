"""
RedisCache.incr: the counter the schema generation is built on (app/schema_registry/manager.py).

Unlike set(), which swallows a failure, incr() says it did not happen: a counter that was not advanced is a
schema change nobody else will see.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.utils import redis_cache


@pytest.fixture
def cache():
    instance = redis_cache.RedisCache.__new__(redis_cache.RedisCache)
    instance._client = MagicMock()
    return instance


def test_incr_returns_the_new_value_and_sets_its_expiry(cache):
    pipe = cache._client.pipeline.return_value
    pipe.execute.return_value = [7, True]
    assert cache.incr("schema_generation:demo", ttl=60) == 7
    pipe.incr.assert_called_once_with(redis_cache._KEY_PREFIX + "schema_generation:demo")
    pipe.expire.assert_called_once_with(redis_cache._KEY_PREFIX + "schema_generation:demo", 60)


def test_incr_says_so_when_redis_cannot_be_reached(cache, caplog):
    cache._client.pipeline.return_value.execute.side_effect = ConnectionError("redis is down")
    with caplog.at_level("WARNING"):
        assert cache.incr("schema_generation:demo") is None
    assert "incr failed" in caplog.text and "schema_generation:demo" in caplog.text


def test_the_counter_reads_back_through_get(cache):
    # incr stores a plain integer; get() parses it as JSON, so the reader gets an int
    cache._client.get.return_value = "7"
    assert cache.get("schema_generation:demo") == 7
