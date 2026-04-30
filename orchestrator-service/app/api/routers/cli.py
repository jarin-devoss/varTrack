"""
app/api/routers/cli.py
──────────────────────
REST endpoints consumed by the vtctl CLI.

Unlike the webhook endpoint which parses a full git platform payload,
these endpoints accept raw file content and run the ETL pipeline directly.
Gateway layer handles JWT auth and RBAC before requests reach here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/cli", tags=["cli"])

# ── Request / Response models ─────────────────────────────────────────────────

class SyncFileRequest(BaseModel):
    datasource: str = Field(..., min_length=1)
    env:        str = Field(..., min_length=1)
    file_path:  str = Field(..., min_length=1)
    content:    str = Field(..., min_length=1)
    format:     Optional[str] = None
    tenant_id:  Optional[str] = None
    dry_run:    bool = False
    label:      Optional[str] = None


class SyncFileResponse(BaseModel):
    task_id: str
    message: str
    dry_run: bool


class ValidateFileRequest(BaseModel):
    file_path:  str = Field(..., min_length=1)
    content:    str = Field(..., min_length=1)
    format:     Optional[str] = None
    datasource: Optional[str] = None
    tenant_id:  Optional[str] = None


class ValidateFileResponse(BaseModel):
    status:    Literal["ok", "warn", "failed"]
    messages:  list[str]
    key_count: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_format(file_path: str, hint: Optional[str]) -> str:
    if hint:
        return hint.lower()
    ext = Path(file_path).suffix.lower().lstrip(".")
    return {
        "yaml": "yaml", "yml": "yaml",
        "json": "json",
        "toml": "toml",
        "env":  "env",
        "ini":  "ini",
        "hcl":  "hcl",
        "properties": "properties",
    }.get(ext, "yaml")


def _resolve_tenant(
    explicit: Optional[str],
    header_value: Optional[str],
) -> str:
    return (explicit or header_value or "default").strip()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/sync", response_model=SyncFileResponse)
async def sync_file(
    body: SyncFileRequest,
    x_tenant_id: Optional[str] = Header(None),
    x_cli_user:  Optional[str] = Header(None),
):
    """
    Accept a raw file and push it through the full ETL pipeline
    (parse → schema-validate → write to sink).

    The datasource must be defined in the CUE bundle.
    When dry_run=true the pipeline executes fully but skips the sink write.
    """
    from app.tasks.etl import process_cli_sync_task

    tenant_id = _resolve_tenant(body.tenant_id, x_tenant_id)
    fmt       = _detect_format(body.file_path, body.format)

    logger.info(
        "cli sync  tenant=%s  datasource=%s  env=%s  file=%s  dry_run=%s  user=%s",
        tenant_id, body.datasource, body.env, body.file_path,
        body.dry_run, x_cli_user or "unknown",
    )

    try:
        task = process_cli_sync_task.delay(
            datasource=body.datasource,
            env=body.env,
            file_path=body.file_path,
            content=body.content,
            fmt=fmt,
            tenant_id=tenant_id,
            dry_run=body.dry_run,
            label=body.label or "",
            submitted_by=x_cli_user or "unknown",
        )
    except Exception as exc:
        logger.exception("cli sync: failed to enqueue task")
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return SyncFileResponse(
        task_id=task.id,
        message="sync task enqueued",
        dry_run=body.dry_run,
    )


@router.post("/validate", response_model=ValidateFileResponse)
async def validate_file(
    body: ValidateFileRequest,
    x_tenant_id: Optional[str] = Header(None),
):
    """
    Parse and validate a file against its CUE schema.
    No data is written to any sink.
    """
    tenant_id = _resolve_tenant(body.tenant_id, x_tenant_id)
    fmt       = _detect_format(body.file_path, body.format)

    from app.parsers.dispatcher import dispatch_parse
    from app.pipeline.validator import Validator
    from app.schema_registry.manager import get_schema_manager

    try:
        flat_data = dispatch_parse(body.content, fmt, body.file_path)
    except Exception as exc:
        return ValidateFileResponse(
            status="failed",
            messages=[f"parse error: {exc}"],
            key_count=0,
        )

    messages: list[str] = []
    status: Literal["ok", "warn", "failed"] = "ok"

    schema_mgr = get_schema_manager(tenant_id)
    if schema_mgr and body.datasource:
        try:
            validator = Validator(schema_mgr)
            v_status, v_messages = validator.validate(
                flat_data=flat_data,
                file_path=body.file_path,
                datasource=body.datasource,
            )
            status   = v_status
            messages = v_messages
        except Exception as exc:
            logger.warning("validate_file: schema validation error: %s", exc)
            messages = [f"schema validation unavailable: {exc}"]
            status   = "warn"

    return ValidateFileResponse(
        status=status,
        messages=messages,
        key_count=len(flat_data),
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Return the current state of a CLI sync task."""
    from app.tasks.status import get_task_status
    result = get_task_status(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    return result


@router.get("/tasks")
async def list_tasks(
    tenant_id: Optional[str] = Query(None),
    limit:     int           = Query(20, ge=1, le=200),
    x_tenant_id: Optional[str] = Header(None),
):
    """List recent CLI sync tasks for a tenant."""
    from app.tasks.status import list_cli_tasks
    t = (tenant_id or x_tenant_id or "default").strip()
    return {"tasks": list_cli_tasks(t, limit=limit)}
