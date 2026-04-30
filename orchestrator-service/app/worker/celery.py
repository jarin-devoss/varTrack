"""
app/worker/celery.py
─────────────────────
Celery application.

Start worker:
    celery -A app.worker.celery worker --queues=webhooks,sync -l info

Start beat (periodic tasks):
    celery -A app.worker.celery beat -l info
"""
from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure
from kombu import Queue

from app.config import settings

_dlq_logger = logging.getLogger(__name__)

celery = Celery(
    "orchestrator",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.etl",
    ],
)

celery.conf.update(
    # serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # timeouts — soft limit raises SoftTimeLimitExceeded (handled in task)
    #             hard limit force-kills the process
    task_soft_time_limit=300,
    task_time_limit=600,
    result_expires=3600,

    # reliability — tasks survive worker crash or redeployment
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # routing
    task_routes={
        "app.tasks.etl.process_webhook_task": {"queue": "webhooks"},
        "app.tasks.etl.refresh_schema_task":  {"queue": "schema"},
        "app.tasks.etl.sync_all_task":        {"queue": "sync"},
    },

    # periodic self-heal every 5 min
    # rules=[] → sync_all_task loads all self_heal=true rules from the bundle at runtime
    # tenant_id=None → sync_all_task iterates ALL registered tenants at runtime
    beat_schedule={
        "sync-all-every-5min": {
            "task":     "app.tasks.etl.sync_all_task",
            "schedule": crontab(minute="*/5"),
            "kwargs":   {"rules": [], "tenant_id": None},
        },
    },

    broker_connection_timeout=5,

    # observability
    worker_send_task_events=True,
    task_send_sent_event=True,

    # worker hygiene — restart after N tasks to prevent memory growth
    worker_max_tasks_per_child=200,
    # prefetch=1 ensures fair distribution; no worker hogs the queue
    worker_prefetch_multiplier=1,

    task_queues=[
        Queue("webhooks"),
        Queue("sync"),
        Queue("schema"),
        Queue("dlq"),   # Dead-letter queue — tasks that exhaust retries land here
    ],
    task_default_queue="webhooks",
)

# Initialise monitoring and connect Celery signal hooks for task metrics.
# Done here (after `celery` is constructed) so signals are active in both
# the worker process and the beat process.
try:
    from app.monitoring import init as monitoring_init
    monitoring_init(
        version=settings.APP_VERSION,
        commit=settings.GIT_COMMIT,
    )
except Exception:
    _dlq_logger.warning("monitoring: init failed in celery worker — metrics unavailable", exc_info=True)


@task_failure.connect
def _route_to_dlq(
    sender=None, task_id=None, exception=None,
    args=None, kwargs=None, traceback=None, einfo=None, **kw
):
    """
    Move a permanently-failed task to the dead-letter queue.

    Celery's task_failure signal fires every time a task raises an unhandled
    exception.  We only route to the DLQ when retries are exhausted (i.e. the
    task has no remaining retries) so normal retry-eligible failures are not
    prematurely DLQ-ed.

    Preserves the original args/kwargs so operators can replay failed tasks
    manually via `celery call` or a management command.
    """
    try:
        if sender is None:
            return
        max_retries = getattr(sender, "max_retries", None)
        request     = getattr(sender, "request", None)
        retries     = getattr(request, "retries", 0) if request else 0
        # Only DLQ when all retries are exhausted (or task has no retry policy)
        if max_retries is not None and retries < max_retries:
            return
        _dlq_logger.error(
            "Task %s[%s] permanently failed after %d retries: %s — routing to DLQ",
            getattr(sender, "name", sender), task_id, retries, exception,
        )
        from celery import current_app
        current_app.send_task(
            getattr(sender, "name", str(sender)),
            args=args or [],
            kwargs=kwargs or {},
            queue="dlq",
        )
    except Exception:
        _dlq_logger.exception("DLQ routing failed for task_id=%s", task_id)
