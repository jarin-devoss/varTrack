"""
tests/unit/test_sync_slots.py

A datasource takes no more syncs at once than it is given (app/pipeline/sync_slots.py).
"""
from __future__ import annotations

import random
import threading
import time
from unittest.mock import patch

import pytest

from app import dependencies
from app.pipeline import sync_slots
from app.pipeline.sync_slots import Limit
from tests.unit._fakes import Clock, FakeRedis

PROD = Limit(name="prod-mongo", datasources=("mongo-primary", "mongo-dr"), max_at_once=2)


@pytest.fixture
def redis():
    return FakeRedis()


# ── Which limit a datasource is under ────────────────────────────────────────

BUNDLE = {"sync_limits": [
    {"name": "prod-mongo", "datasources": ["mongo-primary", "mongo-dr"], "max_at_once": 2},
    {"name": "minio", "datasources": ["s3"], "max_at_once": 4},
]}


def test_the_limits_are_read_from_the_bundle():
    limits = sync_slots.limits_of(BUNDLE)
    assert [(x.name, x.datasources, x.max_at_once) for x in limits] == [
        ("prod-mongo", ("mongo-primary", "mongo-dr"), 2), ("minio", ("s3",), 4)]


def test_a_datasource_is_under_the_limit_that_names_it():
    assert sync_slots.limit_for("mongo-dr", BUNDLE).name == "prod-mongo"
    assert sync_slots.limit_for("s3", BUNDLE).max_at_once == 4


def test_a_datasource_no_limit_names_has_none():
    assert sync_slots.limit_for("redis-hash", BUNDLE) is None
    assert sync_slots.limit_for("mongo", BUNDLE) is None            # "mongo" is not "mongo-primary"


def test_a_bundle_without_limits_limits_nothing():
    assert sync_slots.limit_for("mongo", {}) is None
    assert sync_slots.limits_of(None) == []


def test_an_entry_that_cannot_be_used_is_ignored_not_fatal():
    bundle = {"sync_limits": [{"name": "x", "datasources": ["a"], "max_at_once": 0}, {"datasources": ["b"]},
                              "junk", {"name": "ok", "datasources": ["c"], "max_at_once": 1}]}
    assert [x.name for x in sync_slots.limits_of(bundle)] == ["ok"]


def test_the_bundle_may_spell_the_key_in_camel_case():
    assert sync_slots.limits_of({"sync_limits": [{"name": "x", "datasources": ["a"], "maxAtOnce": 3}]})[0].max_at_once == 3


def test_a_config_that_cannot_be_read_limits_nothing_and_does_not_stop_the_sync():
    with patch("app.pipeline.datasource_settings._bundle", side_effect=RuntimeError("config.cue")):
        assert sync_slots.limit_for("mongo-primary") is None


# ── Taking slots ─────────────────────────────────────────────────────────────

def _acquire(redis, holder, limit=PROD):
    return sync_slots.try_acquire(limit, holder, client=redis, lease=60)


def test_slots_are_given_up_to_the_limit_and_no_further(redis):
    first, _ = _acquire(redis, "t1")
    second, held = _acquire(redis, "t2")
    third, refused_at = _acquire(redis, "t3")
    try:
        assert first and second and held == 2
        assert third is None and refused_at == 2
        assert redis.holders(PROD.key) == ["t1", "t2"]
    finally:
        first.release()
        second.release()


def test_a_released_slot_is_given_to_the_next(redis):
    first, _ = _acquire(redis, "t1")
    second, _ = _acquire(redis, "t2")
    first.release()
    third, held = _acquire(redis, "t3")
    try:
        assert third is not None and held == 2
        assert redis.holders(PROD.key) == ["t2", "t3"]
    finally:
        second.release()
        third.release()


def test_a_holder_that_asks_again_is_not_counted_twice(redis):
    # A task that goes back on the queue and finds its slot still held, and the renewal itself, ask again.
    a, _ = _acquire(redis, "t1")
    b, held = _acquire(redis, "t1")
    try:
        assert held == 1
        other, _ = _acquire(redis, "t2")
        assert other is not None            # one slot is still free
        other.release()
    finally:
        a.release()
        b.release()


def test_a_slot_whose_lease_ran_out_is_free_again(redis):
    # The worker that held it died: nothing renews it.
    dead, _ = sync_slots.try_acquire(PROD, "dead", client=redis, lease=60)
    other, _ = sync_slots.try_acquire(PROD, "other", client=redis, lease=60)
    dead._done.set()                       # stops the renewal thread, as the process dying would
    other._done.set()
    assert _acquire(redis, "full")[0] is None           # both slots are held
    redis.clock.advance(61)
    third, held = _acquire(redis, "t3")
    fourth, _ = _acquire(redis, "t4")
    try:
        assert third is not None and fourth is not None and held == 1
    finally:
        third.release()
        fourth.release()


def test_limits_do_not_share_slots(redis):
    one = Limit("a", ("x",), 1)
    other = Limit("b", ("y",), 1)
    first, _ = sync_slots.try_acquire(one, "t1", client=redis, lease=60)
    second, _ = sync_slots.try_acquire(other, "t2", client=redis, lease=60)
    try:
        assert first is not None and second is not None
    finally:
        first.release()
        second.release()


def test_redis_that_does_not_answer_is_a_lock_unavailable_never_a_free_slot(redis):
    redis.fail = True
    with pytest.raises(dependencies.LockUnavailable, match="sync limit prod-mongo cannot be checked"):
        _acquire(redis, "t1")


def test_a_slot_is_given_back_even_when_redis_is_gone_for_the_moment(redis, caplog):
    slot, _ = _acquire(redis, "t1")
    redis.fail = True
    with caplog.at_level("WARNING", logger="app.pipeline.sync_slots"):
        slot.release()                      # does not raise: the lease runs out by itself
    assert "free when its lease ends" in " ".join(r.getMessage() for r in caplog.records)


def test_a_slot_is_a_context_manager_that_gives_itself_back(redis):
    with _acquire(redis, "t1")[0]:
        assert redis.holders(PROD.key) == ["t1"]
    assert redis.holders(PROD.key) == []


def test_the_lease_is_renewed_while_the_sync_runs(redis):
    slot, _ = sync_slots.try_acquire(PROD, "t1", client=redis, lease=0.08)
    try:
        time.sleep(0.3)                     # several renewals: it renews 3/4 of the way through the lease
    finally:
        slot.release()
    renewals = [c for c in redis.script_calls if c[0] == "acquire" and c[2][0] == "t1"]
    assert len(renewals) >= 3               # the grant, and at least two renewals


def test_a_release_stops_the_renewal(redis):
    slot, _ = sync_slots.try_acquire(PROD, "t1", client=redis, lease=0.08)
    slot.release()
    seen = len(redis.script_calls)
    time.sleep(0.25)
    assert len(redis.script_calls) == seen
    assert not slot._thread.is_alive()


def test_a_renewal_that_fails_says_so_once_and_the_sync_goes_on(redis, caplog):
    slot, _ = sync_slots.try_acquire(PROD, "t1", client=redis, lease=0.06)
    redis.fail = True
    try:
        with caplog.at_level("WARNING", logger="app.pipeline.sync_slots"):
            time.sleep(0.3)
    finally:
        redis.fail = False
        slot.release()
    said = [r.getMessage() for r in caplog.records if "could not be renewed" in r.getMessage()]
    assert len(said) == 1


# ── Waiting ──────────────────────────────────────────────────────────────────

def test_a_free_slot_is_taken_at_once_without_waiting(redis):
    slept: list[float] = []
    slot, _ = sync_slots.wait_for_slot(PROD, "t1", 5, client=redis, sleep=slept.append)
    try:
        assert slot is not None and slept == []
    finally:
        slot.release()


def test_a_sync_asks_again_with_a_growing_pause_until_a_slot_comes_free(redis):
    a, _ = _acquire(redis, "a")
    b, _ = _acquire(redis, "b")
    clock = Clock(0)
    pauses: list[float] = []

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        clock.advance(seconds)
        if len(pauses) == 3:
            a.release()                     # a slot comes free during the third pause

    told: list[int] = []
    slot, held = sync_slots.wait_for_slot(PROD, "c", 5, client=redis, on_wait=told.append, sleep=sleep, clock=clock)
    try:
        assert slot is not None
        assert pauses == [0.1, 0.2, 0.4]
        assert told == [2]                  # told once, with how many hold slots
    finally:
        slot.release()
        b.release()


def test_the_pause_grows_to_a_second_and_stays_there():
    pause = sync_slots.pauses()
    assert [pause() for _ in range(7)] == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0]


def test_a_sync_gives_up_waiting_when_the_time_is_up(redis):
    a, _ = _acquire(redis, "a")
    b, _ = _acquire(redis, "b")
    clock = Clock(0)

    def sleep(seconds: float) -> None:
        clock.advance(seconds)

    try:
        slot, held = sync_slots.wait_for_slot(PROD, "c", 2, client=redis, sleep=sleep, clock=clock)
        assert slot is None and held == 2
        assert 2 <= clock.now <= 2.0001      # it waited the 2 s it was given, and no more
    finally:
        a.release()
        b.release()


def test_a_wait_of_zero_asks_once(redis):
    a, _ = _acquire(redis, "a")
    b, _ = _acquire(redis, "b")
    slept: list[float] = []
    try:
        slot, _ = sync_slots.wait_for_slot(PROD, "c", 0, client=redis, sleep=slept.append)
        assert slot is None and slept == []
    finally:
        a.release()
        b.release()


def test_a_failing_on_wait_does_not_stop_the_wait(redis):
    a, _ = _acquire(redis, "a")
    b, _ = _acquire(redis, "b")
    clock = Clock(0)

    def boom(_: int) -> None:
        raise RuntimeError("ui is down")

    try:
        slot, _ = sync_slots.wait_for_slot(PROD, "c", 0.5, client=redis, on_wait=boom,
                                           sleep=clock.advance, clock=clock)
        assert slot is None
    finally:
        a.release()
        b.release()


# ── Going back on the queue ──────────────────────────────────────────────────

def test_the_requeue_delay_is_between_a_second_and_half_a_minute():
    rng = random.Random(7)
    delays = [sync_slots.requeue_delay(held, rng=rng) for held in (0, 1, 2, 10, 500) for _ in range(200)]
    assert min(delays) >= 1.0 and max(delays) <= 30.0


def test_the_requeue_delays_are_spread_so_refused_syncs_do_not_come_back_together():
    rng = random.Random(1)
    delays = {sync_slots.requeue_delay(4, rng=rng) for _ in range(100)}
    assert len(delays) > 20


def test_more_holders_ahead_means_a_longer_wait_on_average():
    few = [sync_slots.requeue_delay(2, rng=random.Random(i)) for i in range(400)]
    many = [sync_slots.requeue_delay(12, rng=random.Random(i)) for i in range(400)]
    assert sum(many) / len(many) > sum(few) / len(few)


# ── How long a task has been waiting ─────────────────────────────────────────

def test_the_first_ask_is_remembered_and_later_asks_get_the_same_time(redis):
    first = sync_slots.first_wait_at("t1", client=redis, now=100.0)
    later = sync_slots.first_wait_at("t1", client=redis, now=250.0)
    assert first == later == 100.0


def test_a_forgotten_wait_starts_again(redis):
    sync_slots.first_wait_at("t1", client=redis, now=100.0)
    sync_slots.forget_wait("t1", client=redis)
    assert sync_slots.first_wait_at("t1", client=redis, now=300.0) == 300.0


def test_without_redis_the_wait_starts_now_and_nothing_fails(redis):
    redis.fail = True
    assert sync_slots.first_wait_at("t1", client=redis, now=42.0) == 42.0
    sync_slots.forget_wait("t1", client=redis)


def test_the_slot_expired_error_says_which_limit_and_how_full():
    err = sync_slots.SlotWaitExpired(PROD, 2, 600.4)
    assert str(err) == "no slot of sync limit prod-mongo for 600 s (2 of 2 in use)"


def test_two_threads_never_both_get_the_last_slot(redis):
    one = Limit("one", ("x",), 1)
    got: list[sync_slots.Slot] = []
    lock = threading.Lock()

    def ask(i: int) -> None:
        slot, _ = sync_slots.try_acquire(one, f"t{i}", client=redis, lease=60)
        if slot is not None:
            with lock:
                got.append(slot)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(got) == 1
    finally:
        for slot in got:
            slot.release()
