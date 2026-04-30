"""
app/api/routers/dry_run.py
───────────────────────────
POST /v1/webhooks/{datasource}/dry-run

Accepts the same payload as the real webhook endpoint but runs the full
pipeline in simulation mode — no writes, @secret fields masked, prune
candidates shown without deletion.

Response
────────
Synchronous (wait=true, default for small payloads up to timeout) or
asynchronous (task_id for polling via GET /v1/tasks/{task_id}).

Prune flags in rule_config are honoured:
  prune: true          → compute deletion candidates
  prune_last: true     → show which keys would be deferred until last source
  dry_run_prune: true  → always implied in dry-run mode (never actually deletes)
"""
from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request / Response models ─────────────────────────────────────────────────

class DryRunRequest(BaseModel):
    """Optional override body for a dry-run request.

    If rule_config is omitted the orchestrator resolves it from the loaded
    schema bundle exactly as the real webhook handler does.
    """
    rule_config: Optional[dict] = Field(
        default=None,
        description="Override rule config for this dry run. Falls back to bundle resolution.",
    )


class DryRunResponse(BaseModel):
    """Returned when the dry run completes synchronously (task finished inline)."""
    task_id: str
    message: str
    dry_run: bool = True
    received_at: float = Field(default_factory=time.time)
    report: Optional[dict] = None   # populated when wait=true


# ── Helpers ───────────────────────────────────────────────────────────────────

def _platform(headers: dict[str, str]) -> str:
    if "x-github-event" in headers:
        return "github"
    if "x-gitlab-event" in headers:
        return "gitlab"
    if "x-bitbucket-event" in headers:
        return "bitbucket"
    return headers.get("x-platform", "unknown")


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/webhooks/{datasource}/dry-run",
    response_model=DryRunResponse,
    summary="Dry-run webhook ETL",
    description=(
        "Simulate the full ETL pipeline for a webhook payload without writing "
        "to any sink.  @secret-annotated CUE fields are masked in the report. "
        "Prune candidates are computed from the live DB state (read-only)."
    ),
)
async def dry_run_webhook(
    request: Request,
    datasource: Annotated[str, Path(description="Datasource identifier, e.g. 'mongo'")],
    wait: Annotated[
        bool,
        Query(description="Wait for the task to complete and return the full report inline."),
    ] = True,
    timeout: Annotated[
        int,
        Query(description="Max seconds to wait when wait=true (default 30).", ge=1, le=120),
    ] = 30,
) -> DryRunResponse:
    """
    Run the ETL pipeline in dry-run mode.

    Identical to ``POST /v1/webhooks/{datasource}`` but:
    - No data is written to MongoDB (or any sink).
    - Values for fields annotated ``@secret`` in the CUE schema are replaced
      with ``"***"`` in the report.
    - Prune candidates are listed under ``prune.would_delete`` /
      ``prune.deferred_until_last`` without any deletions occurring.
    - The full cost-model breakdown is included for each file.

    Set ``wait=false`` to get back a ``task_id`` immediately and poll via
    ``GET /v1/tasks/{task_id}``.
    """
    from app.tasks.etl import process_webhook_task

    # Read raw bytes directly from the request — accepts any content type
    # and any body shape (JSON object, string, etc.).
    raw_bytes   = await request.body()
    raw_payload = raw_bytes.decode("utf-8", errors="replace")

    received_at = time.time()
    raw_headers = {k.lower(): v for k, v in request.headers.items()}
    platform    = _platform(raw_headers)

    mgr       = getattr(request.app.state, "schema_manager", None)
    # Resolve per-tenant rule_config via X-Tenant-ID header.
    tenant_id = raw_headers.get("x-tenant-id")
    rule_config = (
        mgr.resolve_rule(platform, datasource, tenant_id) if mgr and tenant_id
        else {}
    ) or {}

    # Derive tenant_id from repo owner when not in header or rule_config.
    effective_tenant = tenant_id or rule_config.get("tenant_id")
    if not effective_tenant:
        try:
            import json as _json
            _repo = _json.loads(raw_payload).get("repository", {})
            _full_name = _repo.get("full_name", "") or _repo.get("name", "")
            if _full_name and "/" in _full_name:
                effective_tenant = _full_name.split("/")[0]
                logger.info(
                    "dry_run: tenant_id derived from repo_owner=%s datasource=%s",
                    effective_tenant, datasource,
                )
        except Exception:
            pass

    # Force dry-run flags — merge into the structured prune block regardless of form
    raw_prune = rule_config.get("prune", {})
    if isinstance(raw_prune, bool):
        forced_prune = {"dry_run": True} if raw_prune else {"dry_run": True}
    else:
        forced_prune = {**raw_prune, "dry_run": True}
    rule_config = {**rule_config, "prune": forced_prune, "dry_run": True}

    # ── Circuit breaker: fail-fast when Celery broker is down ────────────────
    from app.utils.circuit_breaker import get_breaker
    breaker = get_breaker()
    if not breaker.allow():
        logger.warning(
            "circuit breaker open — rejecting dry-run datasource=%s platform=%s",
            datasource, platform,
        )
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Celery broker unavailable — circuit breaker open, retry later",
        )

    try:
        task = process_webhook_task.apply_async(
            kwargs={
                "platform":    platform,
                "datasource":  datasource,
                "raw_payload": raw_payload,
                "headers":     dict(request.headers),
                "received_at": received_at,
                "rule_config": rule_config,
                "tenant_id":   effective_tenant or "default",
                "dry_run":     True,
            },
            queue="webhooks",
        )
        breaker.record_success()
    except Exception as _dispatch_exc:
        breaker.record_failure()
        logger.error("dry-run dispatch failed datasource=%s: %s", datasource, _dispatch_exc)
        raise

    logger.info(
        "dry_run accepted task=%s platform=%s datasource=%s",
        task.id, platform, datasource,
    )

    # ── Optionally block and return the full report inline ────────────────────
    if wait:
        try:
            result: dict[str, Any] = task.get(timeout=timeout, propagate=False)
            return DryRunResponse(
                task_id=task.id,
                message="dry_run_complete",
                received_at=received_at,
                report=result,
            )
        except Exception as exc:
            logger.warning("dry_run wait timed out or failed: %s", exc)
            return DryRunResponse(
                task_id=task.id,
                message="dry_run_timeout_poll_task",
                received_at=received_at,
            )

    return DryRunResponse(
        task_id=task.id,
        message="dry_run_accepted",
        received_at=received_at,
    )
