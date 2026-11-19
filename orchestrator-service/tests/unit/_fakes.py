"""
tests/unit/_fakes.py

A Redis that does what app/pipeline/sync_slots.py and write_order.py ask of it, on a clock a test moves.

The Lua script of sync_slots is not run here (there is no Lua in this fake): FakeRedis models what it does, so
the Python around it can be tested. The script itself is run against a real Redis by
tests/integration/test_sync_slots_redis.py.
"""
from __future__ import annotations

import threading
from typing import Any


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedis:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.sets: dict[str, dict[str, float]] = {}     # sorted sets: key -> {member: score}
        self.values: dict[str, str] = {}
        self.fail = False                               # every call raises, as Redis that does not answer
        self.script_calls: list[tuple[str, list, list]] = []
        self._atomic = threading.Lock()      # a script is one step to every other caller, as in Redis

    # ── scripts: a model of the two Lua scripts in sync_slots ────────────────

    def register_script(self, source: str):
        which = "acquire" if "ZSCORE" in source else "count"

        def run(keys: list[str], args: list[Any]):
            with self._atomic:
                return locked(keys, args)

        def locked(keys: list[str], args: list[Any]):
            if self.fail:
                raise ConnectionError("redis is down")
            self.script_calls.append((which, list(keys), list(args)))
            key = keys[0]
            zset = self.sets.setdefault(key, {})
            for member in [m for m, score in zset.items() if score <= self.clock.now]:
                del zset[member]
            if which == "count":
                return len(zset)
            holder, maximum, lease = args[0], int(args[1]), float(args[2])
            if holder in zset:                           # a holder that is already in is renewed, not counted twice
                zset[holder] = self.clock.now + lease
                return [1, len(zset)]
            if len(zset) < maximum:
                zset[holder] = self.clock.now + lease
                return [1, len(zset)]
            return [0, len(zset)]

        return run

    # ── plain commands ────────────────────────────────────────────────────────

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis is down")

    def zrem(self, key: str, member: str) -> int:
        self._check()
        return 1 if self.sets.get(key, {}).pop(member, None) is not None else 0

    def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool | None:
        self._check()
        if nx and key in self.values:
            return None
        self.values[key] = str(value)
        return True

    def get(self, key: str) -> str | None:
        self._check()
        return self.values.get(key)

    def delete(self, key: str) -> int:
        self._check()
        return 1 if self.values.pop(key, None) is not None else 0

    def holders(self, key: str) -> list[str]:
        zset = self.sets.get(key, {})
        return sorted(m for m, score in zset.items() if score > self.clock.now)
