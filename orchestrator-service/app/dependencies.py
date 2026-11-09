"""
app/dependencies.py
───────────────────
What varTrack depends on, and what it says when one of those things fails.

A read of a dependency has three outcomes: a value, a definite absence ("that file is not at that
commit", "no such tenant"), or a failure. A failure is never returned as the value a successful read of
nothing would return (``{}``, ``None``, ``[]``, the input unchanged): the code after it cannot tell the two
apart, and treats an outage as a quiet day. A failure is a ``DependencyError`` that names the dependency
and why, it stops the work it was needed for, and it is recorded here so readiness and the metrics show it.

Each dependency is ``ok``, ``stale`` (a last good value is still in use) or ``unavailable``.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

# A URL with credentials, wherever it turns up in an error's text.
_URL_CREDENTIALS = re.compile(r"(://[^:@/\s]+:)[^@/\s]+(@)")


def scrub(text: object) -> str:
    """*text* with the password of any URL in it hidden: a failed clone's message can carry one."""
    return _URL_CREDENTIALS.sub(r"\1***\2", str(text))


class DependencyError(RuntimeError):
    """Something varTrack needs could not be read, so the work that needed it did not happen."""

    #: the registry name of the dependency: "config.cue", "git", "secrets", "schemas"
    dependency = "dependency"

    def __init__(self, reason: str, *, what: str = "") -> None:
        self.reason = scrub(reason)
        self.what = what
        super().__init__(self.reason)


class BundleUnavailable(DependencyError):
    """config.cue cannot be read and there is no earlier reading to keep using."""
    dependency = "config.cue"


class SourceUnavailable(DependencyError):
    """A Git repository could not be cloned or fetched (as opposed to: the file is not in it)."""
    dependency = "git"


class SecretUnavailable(DependencyError):
    """A secret that a file or a datasource needs could not be read from its secret manager."""
    dependency = "secrets"


class LockUnavailable(DependencyError):
    """The lock that keeps two syncs of one place apart cannot be taken (Redis does not answer)."""
    dependency = "redis"


class SchemaUnavailable(DependencyError):
    """A tenant's schema repository could not be read."""
    dependency = "schemas"


@dataclass(frozen=True)
class Status:
    state: str                  # "ok" | "stale" | "unavailable"
    reason: str = ""
    since: float = 0.0


_lock = threading.Lock()
_status: dict[str, Status] = {}


def up(name: str) -> None:
    with _lock:
        _status[name] = Status("ok", "", time.time())
        if name == "config.cue":
            _modes.pop("config", None)      # config.cue reads again: the mode is over
    _metric(name, True)


def stale(name: str, reason: str) -> None:
    """The dependency is failing, and the last good value is in use."""
    _set(name, "stale", reason)


def down(name: str, reason: str) -> None:
    _set(name, "unavailable", reason)


def _set(name: str, state: str, reason: str) -> None:
    with _lock:
        previous = _status.get(name)
        since = previous.since if previous and previous.state == state else time.time()
        _status[name] = Status(state, scrub(reason), since)
    _metric(name, state != "unavailable")


def _metric(name: str, is_up: bool) -> None:
    """The vartrack_dependency_up gauge; a service whose monitoring is not up has nothing to set."""
    try:
        from app.monitoring import get_metrics
        metrics = get_metrics()
        if metrics is not None:
            metrics.set_dependency_up(name, is_up)
    except Exception:
        pass


def get(name: str) -> Optional[Status]:
    with _lock:
        return _status.get(name)


def snapshot() -> dict[str, dict[str, object]]:
    """Every dependency that has been checked, for readiness: {name: {state, reason, since}}."""
    with _lock:
        return {name: {"state": s.state, "reason": s.reason, "since": s.since} for name, s in sorted(_status.items())}


# What the service cannot be ready without: everything else is read from config.cue.
REQUIRED = ("config.cue",)


def readiness() -> dict[str, object]:
    """What readiness adds to its own checks: whether a required dependency is unavailable, every
    dependency that has been checked, and every degraded mode that is on."""
    accepted = {"config.cue"} if "config" in active_modes() else set()   # the operator said it may be down
    blocking = [name for name in REQUIRED
                if name not in accepted and (get(name) or Status("ok")).state == "unavailable"]
    out: dict[str, object] = {"dependencies": snapshot(), "degraded": active_modes()}
    if blocking:
        out["unavailable"] = blocking
    return out


def unavailable() -> list[str]:
    with _lock:
        return sorted(name for name, s in _status.items() if s.state == "unavailable")


# ── Degraded modes ────────────────────────────────────────────────────────────
#
# A weaker mode exists only when an operator names it in VARTRACK_ALLOW_DEGRADED (comma-separated; an
# environment variable, because "config" is the mode for a config.cue that cannot be read, which cannot
# be configured in config.cue). It is logged when it is taken and listed by readiness for as long as it
# is on.

MODES = {
    "config":            "start without config.cue, on environment variables only",
    "nonce_store":       "keep webhook replay protection in this process instead of Redis",
    "tls":               "serve without TLS when no certificate is available",
    "schema_validation": "write files unvalidated when the cue program is missing",
}

# Never allowed in production, whatever is named.
NEVER_IN_PRODUCTION = frozenset({"nonce_store", "tls"})

_modes: dict[str, str] = {}


def production() -> bool:
    return os.getenv("APP_ENV", "").strip().lower() == "production"


_production = production


def allowed(mode: str) -> bool:
    """Whether the operator named *mode*, and it may be used here."""
    named = {m.strip() for m in os.getenv("VARTRACK_ALLOW_DEGRADED", "").split(",") if m.strip()}
    unknown = named - set(MODES)
    if unknown:
        raise ValueError(f"VARTRACK_ALLOW_DEGRADED names {', '.join(sorted(unknown))}: "
                         f"the modes are {', '.join(sorted(MODES))}")
    return mode in named and not (mode in NEVER_IN_PRODUCTION and _production())


def mode_on(mode: str, why: str) -> None:
    """Record that *mode* is in use, and why. Only after allowed(mode) said so."""
    with _lock:
        _modes[mode] = scrub(why)


def active_modes() -> dict[str, str]:
    with _lock:
        return dict(_modes)


def reset() -> None:
    """For tests."""
    with _lock:
        _status.clear()
        _modes.clear()
