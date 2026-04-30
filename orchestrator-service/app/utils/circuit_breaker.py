"""
app/utils/circuit_breaker.py
─────────────────────────────
3-state circuit breaker: Closed → Open → HalfOpen → Closed.

halfOpenAllowed is decremented after each success in HalfOpen state so
subsequent probes can proceed (prevents deadlock where successCount can
never reach success_threshold).

States
──────
  Closed    – normal operation; failures are counted
  Open      – all requests fail-fast with CircuitOpenError
  HalfOpen  – probing; limited requests allowed

Transitions
────────────
  Closed   → Open      : failureCount  >= failure_threshold
  Open     → HalfOpen  : time elapsed  >= open_duration
  HalfOpen → Closed    : successCount  >= success_threshold
  HalfOpen → Open      : any single failure resets the probe
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half-open"


class CircuitOpenError(RuntimeError):
    """Raised by CircuitBreaker.allow() when the circuit is open."""


_DEFAULTS = dict(
    failure_threshold=5,
    success_threshold=3,
    open_duration=30.0,      # seconds
    half_open_max_allowed=1,
)


class CircuitBreaker:
    """
    Thread-safe 3-state circuit breaker.

    Usage::

        breaker = CircuitBreaker()   # or CircuitBreaker(**custom_config)

        # In webhook handler:
        if not breaker.allow():
            raise HTTPException(503, "broker unavailable — circuit open")
        try:
            task = some_celery_task.apply_async(...)
            breaker.record_success()
        except Exception:
            breaker.record_failure()
            raise
    """

    def __init__(
        self,
        failure_threshold:   int   = _DEFAULTS["failure_threshold"],  # type: ignore[assignment]
        success_threshold:   int   = _DEFAULTS["success_threshold"],  # type: ignore[assignment]
        open_duration:       float = _DEFAULTS["open_duration"],
        half_open_max_allowed: int = _DEFAULTS["half_open_max_allowed"],  # type: ignore[assignment]
    ) -> None:
        self._failure_threshold    = failure_threshold
        self._success_threshold    = success_threshold
        self._open_duration        = open_duration
        self._half_open_max        = half_open_max_allowed

        self._state:             CircuitState = CircuitState.CLOSED
        self._failure_count:     int   = 0
        self._success_count:     int   = 0
        self._half_open_allowed: int   = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def allow(self) -> bool:
        """
        Return True if the request should proceed.

        Does NOT raise — callers check the bool and raise their own HTTP 503
        (or call allow_or_raise() for the raising variant).
        """
        with self._lock:
            return self._check_allow()

    def allow_or_raise(self) -> None:
        """
        Like allow() but raises CircuitOpenError when the circuit is open.
        Convenience for code that prefers exception flow.
        """
        if not self.allow():
            raise CircuitOpenError(
                f"Circuit breaker is {self._state.value} — request rejected"
            )

    def record_success(self) -> None:
        """Record that the last request succeeded."""
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                # Decrement half_open_allowed so the next probe slot opens.
                self._half_open_allowed -= 1
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self._state is CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record that the last request failed."""
        with self._lock:
            if self._state is CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._last_failure_time = time.monotonic()
                    self._transition_to(CircuitState.OPEN)
            elif self._state is CircuitState.HALF_OPEN:
                self._last_failure_time = time.monotonic()
                self._transition_to(CircuitState.OPEN)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def state_dict(self) -> dict[str, Any]:
        """Return current state as a JSON-serialisable dict (for /healthz)."""
        with self._lock:
            return {
                "state":            self._state.value,
                "failure_count":    self._failure_count,
                "success_count":    self._success_count,
                "last_failure_age": (
                    round(time.monotonic() - self._last_failure_time, 1)
                    if self._last_failure_time else None
                ),
            }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _check_allow(self) -> bool:
        """Must be called with self._lock held."""
        if self._state is CircuitState.CLOSED:
            return True

        if self._state is CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed > self._open_duration:
                self._transition_to(CircuitState.HALF_OPEN)
                self._half_open_allowed = 1
                return True
            return False

        if self._state is CircuitState.HALF_OPEN:
            if self._half_open_allowed < self._half_open_max:
                self._half_open_allowed += 1
                return True
            return False

        return True  # unknown state — allow

    def _transition_to(self, new_state: CircuitState) -> None:
        """Must be called with self._lock held."""
        old = self._state
        self._state            = new_state
        self._failure_count    = 0
        self._success_count    = 0
        self._half_open_allowed = 0
        logger.info(
            "circuit_breaker state=%s (was %s)", new_state.value, old.value
        )


# ── Module-level singleton ────────────────────────────────────────────────────
# Shared across all webhook requests in the same process.
# Protects process_webhook_task.apply_async() from a downed Celery broker.

_breaker: CircuitBreaker | None = None
_breaker_lock = threading.Lock()


def get_breaker() -> CircuitBreaker:
    """Return the module-level singleton, creating it on first call."""
    global _breaker
    if _breaker is None:
        with _breaker_lock:
            if _breaker is None:
                _breaker = CircuitBreaker()
    return _breaker
