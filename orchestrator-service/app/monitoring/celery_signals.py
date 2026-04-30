"""
app/monitoring/celery_signals.py
──────────────────────────────────
Celery signal hooks that feed task metrics into OrchestratorMetrics.

Signals used (Celery docs pattern):
  task_prerun   → record start time in task.request
  task_postrun  → observe duration + inc success counter
  task_failure  → observe duration + inc failure counter
  task_retry    → inc retry counter
  task_revoked  → inc failure counter (revoked = aborted)

Wiring: import this module once in app/worker/celery.py after the Celery
app is constructed.  The @signal.connect decorators register themselves
at import time — no explicit wiring call needed beyond the import.

Connect to task signals to record timing without modifying task code itself.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def _get_metrics():
    """Lazy-import metrics so this module can be imported before init()."""
    from app.monitoring import get_metrics
    return get_metrics()


def _task_queue(request) -> str:
    """Extract queue name from a Celery task request, defaulting to 'webhooks'."""
    return getattr(request, "delivery_info", {}).get("routing_key", "webhooks") or "webhooks"


# ── Connect all signals ───────────────────────────────────────────────────────

def connect_signals() -> None:
    """
    Register all Celery signal handlers.

    Call this once after the Celery app is constructed (in celery.py after
    `celery = Celery(...)`) so signals are active for both the API process
    (task dispatch) and the worker process (task execution).
    """
    from celery.signals import (
        task_prerun,
        task_postrun,
        task_failure,
        task_retry,
        task_revoked,
    )

    @task_prerun.connect
    def _on_task_prerun(task_id=None, task=None, args=None, kwargs=None, **kw):
        """Record task start time on the request object for later duration calc."""
        if task is not None and hasattr(task, "request"):
            task.request._vt_start = time.perf_counter()

    @task_postrun.connect
    def _on_task_postrun(
        task_id=None, task=None, args=None, kwargs=None,
        retval=None, state=None, **kw,
    ):
        """Record task duration and success counter."""
        metrics = _get_metrics()
        if metrics is None or task is None:
            return
        start = getattr(getattr(task, "request", None), "_vt_start", None)
        elapsed = time.perf_counter() - start if start is not None else 0.0
        task_name = getattr(task, "name", "unknown")
        queue     = _task_queue(getattr(task, "request", None))

        metrics.observe_task_duration(task_name, queue, elapsed)
        # state is CELERY state string: SUCCESS, FAILURE, etc.
        if state and state.upper() == "SUCCESS":
            metrics.inc_task(task_name, queue, "success")

    @task_failure.connect
    def _on_task_failure(
        task_id=None, exception=None, args=None, kwargs=None,
        traceback=None, einfo=None, sender=None, **kw,
    ):
        """Record failure counter (and duration if available)."""
        metrics = _get_metrics()
        if metrics is None or sender is None:
            return
        start = getattr(getattr(sender, "request", None), "_vt_start", None)
        elapsed = time.perf_counter() - start if start is not None else 0.0
        task_name = getattr(sender, "name", "unknown")
        queue     = _task_queue(getattr(sender, "request", None))

        metrics.observe_task_duration(task_name, queue, elapsed)

        # Determine if this is a DLQ routing (retries exhausted) or a normal failure.
        max_retries = getattr(sender, "max_retries", None)
        request     = getattr(sender, "request", None)
        retries     = getattr(request, "retries", 0) if request else 0
        if max_retries is not None and retries >= max_retries:
            metrics.inc_task(task_name, queue, "dlq")
        else:
            metrics.inc_task(task_name, queue, "failure")

    @task_retry.connect
    def _on_task_retry(request=None, reason=None, einfo=None, **kw):
        """Record a retry attempt."""
        metrics = _get_metrics()
        if metrics is None or request is None:
            return
        task_name = getattr(request, "task", "unknown")
        metrics.inc_task_retry(task_name)
        queue = _task_queue(request)
        metrics.inc_task(task_name, queue, "retry")

    @task_revoked.connect
    def _on_task_revoked(request=None, terminated=None, signum=None, expired=None, **kw):
        """Record revoked (aborted) tasks as failures."""
        metrics = _get_metrics()
        if metrics is None or request is None:
            return
        task_name = getattr(request, "task", "unknown")
        queue     = _task_queue(request)
        metrics.inc_task(task_name, queue, "failure")

    logger.debug("monitoring: Celery signal handlers connected")
