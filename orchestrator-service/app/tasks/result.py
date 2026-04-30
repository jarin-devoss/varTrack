"""
app/tasks/result.py
────────────────────
Small helpers to build consistent Celery task result dicts.
"""
from __future__ import annotations

from typing import Any

from app.utils.enums.task_state import TaskState


def ok(task_id: str, message: str = "", **data: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "state":   TaskState.SUCCEEDED.name,
        "message": message,
        **data,
    }


def fail(task_id: str, message: str = "") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "state":   TaskState.FAILED.name,
        "message": message,
    }
