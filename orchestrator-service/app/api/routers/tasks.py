"""
app/api/routers/tasks.py
─────────────────────────
GET /v1/tasks/{task_id}   →  poll Celery task state
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


class TaskStatusResponse(BaseModel):
    task_id: str
    state:   str
    result:  dict | None = None


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str) -> TaskStatusResponse:
    if not _UUID_RE.match(task_id):
        raise HTTPException(status_code=404, detail="Task not found")

    from app.worker.celery import celery
    result = celery.AsyncResult(task_id)

    if result.state == "PENDING":
        return TaskStatusResponse(task_id=task_id, state="PENDING")

    if result.state == "FAILURE":
        raise HTTPException(status_code=500, detail=str(result.info))

    return TaskStatusResponse(
        task_id=task_id,
        state=result.state,
        result=result.result if isinstance(result.result, dict) else None,
    )
