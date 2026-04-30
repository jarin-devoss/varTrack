"""
app/celery_app/celery.py
─────────────────────────
Celery application factory — backward-compat alias for app.worker.celery.

The canonical Celery app lives in app/worker/celery.py (see there for full
config).  This module re-exports it so code that imports from either path
gets the same singleton.

Usage:
    from app.celery_app.celery import celery_app   # legacy callers
    from app.worker.celery import celery            # preferred
"""
from __future__ import annotations

from app.worker.celery import celery as celery_app  # noqa: F401

# Convenience alias — both names refer to the same Celery instance.
app = celery_app
