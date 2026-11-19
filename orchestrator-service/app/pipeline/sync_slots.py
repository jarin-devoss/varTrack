"""
app/pipeline/sync_slots.py
───────────────────────────
A datasource takes no more syncs at once than it is given.

``sync_limits`` in config.cue names groups of datasources and how many syncs may write to a group at
once, across every worker:

    sync_limits: [{name: "prod-mongo", datasources: ["mongo-primary", "mongo-dr"], max_at_once: 2}]

A sync that is about to write takes a slot of its limit and gives it back when it is done. The slots
of a limit are the members of one Redis sorted set, each a holder (the task id) scored by when its lease
runs out, in Redis's own clock: the container clocks here step back, and a lease that expires by
a different clock than the one that granted it would be wrong. The lease is renewed while the sync runs, so a
long sync keeps its slot, and a worker that dies stops renewing, so its slot is free again after
SYNC_SLOT_LEASE_SECONDS: no daemon has to sweep it.

A sync that finds no free slot waits a few seconds with a growing pause, then goes back on the queue
(app/tasks/etl.py), so it does not hold a pool process while it waits.

Only writes take a slot. The ETL, a dry run and every read are not limited.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

KEY_PREFIX = "vt:sync-slots:"

# acquire: one script, so the check and the grant cannot be split by another worker.
#   KEYS[1]  the limit's sorted set
#   ARGV     holder, max_at_once, lease seconds
#   returns  {granted (1 or 0), holders now}
# A holder that is already in the set is renewed, not counted twice: a task that goes back on the queue and
# comes back with its slot still held, and the renewal thread, both go through here.
_ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local lease = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], now + lease, ARGV[1])
  redis.call('EXPIRE', KEYS[1], math.ceil(lease) + 5)
  return {1, redis.call('ZCARD', KEYS[1])}
end
local held = redis.call('ZCARD', KEYS[1])
if held < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], now + lease, ARGV[1])
  redis.call('EXPIRE', KEYS[1], math.ceil(lease) + 5)
  return {1, held + 1}
end
return {0, held}
"""

# How many holders there are now, without taking one (for the metrics and the UI).
_COUNT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""


@dataclass(frozen=True)
class Limit:
    """One entry of config.cue's sync_limits."""
    name: str
    datasources: tuple[str, ...]
    max_at_once: int

    @property
    def key(self) -> str:
        return KEY_PREFIX + self.name


class SlotWaitExpired(RuntimeError):
    """A sync has waited for a slot as long as it is allowed to."""

    def __init__(self, limit: Limit, in_use: int, waited: float) -> None:
        self.limit, self.in_use, self.waited = limit, in_use, waited
        super().__init__(f"no slot of sync limit {limit.name} for {waited:.0f} s "
                         f"({in_use} of {limit.max_at_once} in use)")


# ── Which limit a datasource is under ────────────────────────────────────────

def limits_of(bundle: Mapping[str, Any] | None) -> list[Limit]:
    """The bundle's sync_limits, as the loader prints them (the loader has checked them)."""
    out: list[Limit] = []
    for entry in (bundle or {}).get("sync_limits") or []:
        if not isinstance(entry, Mapping):
            continue
        try:
            out.append(Limit(
                name=str(entry["name"]),
                datasources=tuple(str(d) for d in entry.get("datasources") or ()),
                max_at_once=int(entry.get("max_at_once", entry.get("maxAtOnce", 0))),
            ))
        except (KeyError, TypeError, ValueError):
            logger.warning("sync_limits: an entry could not be read and is ignored: %r", entry)
    return [limit for limit in out if limit.max_at_once >= 1]


def limit_for(datasource: str, bundle: Mapping[str, Any] | None = None) -> Limit | None:
    """The limit *datasource* is under, or None. *bundle* is read from config.cue when not given."""
    if bundle is None:
        try:
            from app.pipeline.datasource_settings import _bundle
            bundle = _bundle()
        except Exception:
            logger.warning("sync_limits: config.cue could not be read; no limit applies to %s", datasource,
                           exc_info=True)
            return None
    for limit in limits_of(bundle):
        if datasource in limit.datasources:
            return limit
    return None


# ── The slot ──────────────────────────────────────────────────────────────────

def _client() -> Any:
    from app.ui import ledger
    return ledger._redis()


def _lease_seconds() -> float:
    from app.config import settings
    return float(settings.SYNC_SLOT_LEASE_SECONDS)


class Slot:
    """A slot held by *holder*. A thread renews its lease while it is held; release() gives it back."""

    def __init__(self, client: Any, limit: Limit, holder: str, lease: float, in_use: int) -> None:
        self._client = client
        self.limit = limit
        self.holder = holder
        self.lease = lease
        self.in_use = in_use                 # holders at the moment this one was granted (itself included)
        self._acquire = client.register_script(_ACQUIRE)
        self._done = threading.Event()
        self._lost_said = False
        self._thread = threading.Thread(target=self._renew, name=f"sync-slot-{limit.name}", daemon=True)
        self._thread.start()

    def _renew(self) -> None:
        # 3/4 of the way through the lease, so one missed renewal still leaves time for the next.
        while not self._done.wait(self.lease * 0.75):
            try:
                granted, _ = self._acquire(keys=[self.limit.key],
                                           args=[self.holder, self.limit.max_at_once, self.lease])
                if not granted and not self._lost_said:
                    self._lost_said = True
                    logger.warning("sync limit %s: the slot of %s could not be renewed, so another sync may have "
                                   "taken it; this sync goes on", self.limit.name, self.holder)
            except Exception as exc:
                if not self._lost_said:
                    self._lost_said = True
                    logger.warning("sync limit %s: the slot of %s could not be renewed (%s); the sync goes on and "
                                   "the limit may be exceeded", self.limit.name, self.holder, exc)

    def release(self) -> None:
        self._done.set()
        try:
            self._client.zrem(self.limit.key, self.holder)
        except Exception as exc:
            # The lease runs out by itself: nothing is held for good.
            logger.warning("sync limit %s: the slot of %s could not be given back (%s); it is free when its "
                           "lease ends", self.limit.name, self.holder, exc)

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def try_acquire(limit: Limit, holder: str, *, client: Any = None, lease: float | None = None) -> tuple[Slot | None, int]:
    """Take a slot of *limit* for *holder* if one is free. Returns (the slot or None, holders now).
    Raises dependencies.LockUnavailable when Redis cannot be asked: a write is never made without the check."""
    from app import dependencies

    client = client if client is not None else _client()
    lease = _lease_seconds() if lease is None else lease
    try:
        script = client.register_script(_ACQUIRE)
        granted, held = script(keys=[limit.key], args=[holder, limit.max_at_once, lease])
    except Exception as exc:
        dependencies.down("redis", f"sync limit {limit.name} cannot be checked: {exc}")
        raise dependencies.LockUnavailable(f"sync limit {limit.name} cannot be checked: {exc}") from None
    held = int(held)
    if not granted:
        return None, held
    return Slot(client, limit, holder, lease, held), held


def in_use(limit: Limit, *, client: Any = None) -> int | None:
    """Holders of *limit* now, or None when Redis cannot say."""
    try:
        client = client if client is not None else _client()
        return int(client.register_script(_COUNT)(keys=[limit.key], args=[]))
    except Exception:
        return None


def pauses() -> Callable[[], float]:
    """The pause between asks for a slot: 0.1 s, growing to 1 s. A step up, not a hammering."""
    steps = iter((0.1, 0.2, 0.4, 0.8))
    return lambda: next(steps, 1.0)


def wait_for_slot(
    limit: Limit,
    holder: str,
    wait_seconds: float,
    *,
    client: Any = None,
    on_wait: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Slot | None, int]:
    """Take a slot of *limit*, asking again with a growing pause for at most *wait_seconds*.
    Returns (the slot or None, holders at the last ask). *on_wait* is told how many hold slots, once, the
    first time none is free."""
    deadline = clock() + max(0.0, wait_seconds)
    pause = pauses()
    told = False
    while True:
        slot, held = try_acquire(limit, holder, client=client)
        if slot is not None:
            return slot, held
        if not told and on_wait is not None:
            told = True
            try:
                on_wait(held)
            except Exception:
                logger.debug("sync limit %s: on_wait failed", limit.name, exc_info=True)
        remaining = deadline - clock()
        if remaining <= 0:
            return None, held
        sleep(min(pause(), remaining))


def requeue_delay(held: int, *, rng: random.Random | None = None) -> float:
    """Seconds before a sync that found no slot is tried again: random around how long the holders ahead are
    likely to take (about a second each), so the syncs that were refused together do not come back together."""
    mean = min(15.0, max(2.0, float(held)))
    draw = (rng or random).expovariate(1.0 / mean)
    return round(min(30.0, max(1.0, draw)), 1)


# ── Said once, on the first ask of a task ────────────────────────────────────

def first_wait_at(holder: str, *, client: Any = None, now: float | None = None) -> float:
    """The time (epoch seconds) this task first asked for a slot. The first ask records it, so a task that goes
    back on the queue and comes back still knows how long it has been waiting."""
    now = time.time() if now is None else now
    try:
        client = client if client is not None else _client()
        key = f"{KEY_PREFIX}waiting:{holder}"
        client.set(key, repr(now), nx=True, ex=3600)
        return float(client.get(key) or now)
    except Exception:
        return now


def forget_wait(holder: str, *, client: Any = None) -> None:
    try:
        (client if client is not None else _client()).delete(f"{KEY_PREFIX}waiting:{holder}")
    except Exception:
        pass
