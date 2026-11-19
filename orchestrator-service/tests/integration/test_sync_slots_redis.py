"""
tests/integration/test_sync_slots_redis.py

The Lua script of app/pipeline/sync_slots.py, run by a real Redis.

The unit tests (tests/unit/test_sync_slots.py) run the Python around the script against a model of it; only
a Redis can say the script does what the model does: the grant and the count in one step, the lease timed by
Redis's own clock, a holder that asks again renewed and not counted twice.

Skipped unless VARTRACK_TEST_REDIS names a Redis to use (a throwaway one: its keys under vt:sync-slots:test-* are
used and removed):

    docker run --rm -d -p 16379:6379 redis:7-alpine
    VARTRACK_TEST_REDIS=redis://localhost:16379/0 python -m pytest tests/integration/test_sync_slots_redis.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid

import pytest

URL = os.environ.get("VARTRACK_TEST_REDIS", "")

# tests/conftest.py puts a stand-in for redis in sys.modules before any test is imported; this file needs the
# real client, only to hand to the code under test, so it takes it and puts the stand-in back.
_stand_in = sys.modules.pop("redis", None)
try:
    import redis as real_redis
except ImportError:                                           # pragma: no cover
    real_redis = None
finally:
    if _stand_in is not None:
        sys.modules["redis"] = _stand_in

pytestmark = pytest.mark.skipif(not URL or real_redis is None, reason="VARTRACK_TEST_REDIS is not set")

from app.pipeline import sync_slots  # noqa: E402
from app.pipeline.sync_slots import Limit  # noqa: E402


@pytest.fixture
def client():
    c = real_redis.Redis.from_url(URL, decode_responses=True)
    yield c
    for key in c.scan_iter("vt:sync-slots:test-*"):
        c.delete(key)
    c.close()


@pytest.fixture
def limit():
    return Limit(name=f"test-{uuid.uuid4().hex[:8]}", datasources=("x",), max_at_once=2)


def _take(client, limit, holder, lease=30):
    return sync_slots.try_acquire(limit, holder, client=client, lease=lease)


def test_the_slots_of_a_limit_are_given_up_to_max_at_once(client, limit):
    a, _ = _take(client, limit, "a")
    b, held = _take(client, limit, "b")
    c, refused_at = _take(client, limit, "c")
    try:
        assert a and b and held == 2
        assert c is None and refused_at == 2
    finally:
        a.release()
        b.release()


def test_a_released_slot_is_given_to_the_next(client, limit):
    a, _ = _take(client, limit, "a")
    b, _ = _take(client, limit, "b")
    a.release()
    c, held = _take(client, limit, "c")
    try:
        assert c is not None and held == 2
        assert set(client.zrange(limit.key, 0, -1)) == {"b", "c"}
    finally:
        b.release()
        c.release()


def test_a_holder_that_asks_again_is_renewed_and_counted_once(client, limit):
    a, _ = _take(client, limit, "a")
    again, held = _take(client, limit, "a")
    b, _ = _take(client, limit, "b")
    try:
        assert held == 1 and b is not None
        assert client.zcard(limit.key) == 2
    finally:
        a.release()
        again.release()
        b.release()


def test_a_lease_that_runs_out_is_timed_by_redis_and_frees_the_slot(client, limit):
    one = Limit(limit.name, limit.datasources, 1)
    dead, _ = sync_slots.try_acquire(one, "dead", client=client, lease=1)
    dead._done.set()                                          # the holder died: nothing renews it
    assert sync_slots.try_acquire(one, "next", client=client, lease=1)[0] is None
    time.sleep(1.4)
    next_slot, held = sync_slots.try_acquire(one, "next", client=client, lease=30)
    try:
        assert next_slot is not None and held == 1
    finally:
        next_slot.release()


def test_the_score_of_a_slot_is_redis_time_plus_the_lease(client, limit):
    slot, _ = _take(client, limit, "a", lease=30)
    try:
        redis_now = client.time()
        score = client.zscore(limit.key, "a")
        assert redis_now[0] + 28 <= score <= redis_now[0] + 31
    finally:
        slot.release()


def test_the_key_of_an_idle_limit_expires_by_itself(client, limit):
    slot, _ = _take(client, limit, "a", lease=30)
    slot.release()
    _take(client, limit, "b", lease=30)[0].release()
    ttl = client.ttl(limit.key)
    assert ttl == -2 or 0 < ttl <= 35


def test_a_renewed_lease_keeps_a_slot_for_longer_than_its_first_lease(client, limit):
    one = Limit(limit.name, limit.datasources, 1)
    slot, _ = sync_slots.try_acquire(one, "long", client=client, lease=1)
    try:
        time.sleep(2.3)                                       # twice the lease: only the renewal kept it
        assert sync_slots.try_acquire(one, "other", client=client, lease=1)[0] is None
    finally:
        slot.release()


def test_many_threads_never_hold_more_slots_than_the_limit(client, limit):
    one = Limit(limit.name, limit.datasources, 3)
    granted: list = []
    lock = threading.Lock()

    def ask(i: int) -> None:
        own = real_redis.Redis.from_url(URL, decode_responses=True)
        slot, _ = sync_slots.try_acquire(one, f"t{i}", client=own, lease=30)
        if slot is not None:
            with lock:
                granted.append(slot)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(granted) == 3
        assert client.zcard(limit.key) == 3
    finally:
        for slot in granted:
            slot.release()


def test_the_count_does_not_take_a_slot(client, limit):
    assert sync_slots.in_use(limit, client=client) == 0
    a, _ = _take(client, limit, "a")
    try:
        assert sync_slots.in_use(limit, client=client) == 1
        assert sync_slots.in_use(limit, client=client) == 1
    finally:
        a.release()
    assert sync_slots.in_use(limit, client=client) == 0
