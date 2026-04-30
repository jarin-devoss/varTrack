"""
app/api/routers/webhooks.py
────────────────────────────
POST /v1/webhooks/{datasource}      →  process_webhook_task
POST /v1/webhooks/schema-registry   →  refresh_schema_task
"""
from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


class WebhookResponse(BaseModel):
    task_id: str
    message: str
    received_at: float = Field(default_factory=time.time)


class SchemaWebhookBody(BaseModel):
    platform:    str
    repo:        str
    branch:      str
    raw_payload: str = ""   # optional: raw git push payload for logging
    tenant_id:   str


def _platform(headers: dict[str, str]) -> str:
    if "x-github-event" in headers:
        return "github"
    if "x-gitlab-event" in headers:
        return "gitlab"
    if "x-bitbucket-event" in headers:
        return "bitbucket"
    return headers.get("x-platform", "unknown")


@router.post("/webhooks/schema-registry", response_model=WebhookResponse)
async def schema_webhook(request: Request, body: SchemaWebhookBody) -> WebhookResponse:
    """Refresh schema registry when the schema repo receives a push."""
    from app.tasks.etl import refresh_schema_task
    task = refresh_schema_task.apply_async(
        kwargs={
            "platform":    body.platform,
            "repo":        body.repo,
            "branch":      body.branch,
            "raw_payload": body.raw_payload,
            "headers":     dict(request.headers),
            "tenant_id":   body.tenant_id,
        },
        queue="schema",
    )
    return WebhookResponse(task_id=task.id, message="schema_accepted")


@router.post("/webhooks/{datasource}", response_model=WebhookResponse)
async def ingest_webhook(
    request: Request,
    datasource: Annotated[str, Path(description="Datasource identifier, e.g. 'mongo'")],
) -> WebhookResponse:
    """Ingest a git-push webhook and enqueue an ETL task."""
    from app.tasks.etl import process_webhook_task

    received_at = time.time()
    raw_bytes   = await request.body()
    raw_payload = raw_bytes.decode("utf-8", errors="replace")

    raw_headers = {k.lower(): v for k, v in request.headers.items()}
    platform    = _platform(raw_headers)

    # Resolve rule_config per-tenant using X-Tenant-ID header so the correct
    # rule_config is returned for every tenant in a multi-tenant deployment.
    tenant_id_hdr = raw_headers.get("x-tenant-id")
    mgr         = getattr(request.app.state, "schema_manager", None)
    rule_config = (
        mgr.resolve_rule(platform, datasource, tenant_id_hdr)
        if mgr and tenant_id_hdr
        else (mgr.resolve_rule(platform, datasource) if mgr else {})
    )
    rule_config  = rule_config or {}

    # Resolve tenant_id: prefer rule_config, then fall back to the repository
    # owner extracted from the webhook payload (Renovate topLevelOrg pattern).
    # e.g. payload repository.full_name = "acme/infra" → tenant_id = "acme"
    tenant_id = rule_config.get("tenant_id")
    if not tenant_id:
        try:
            import json as _json
            _repo = _json.loads(raw_payload).get("repository", {})
            _full_name = _repo.get("full_name", "") or _repo.get("name", "")
            if _full_name and "/" in _full_name:
                tenant_id = _full_name.split("/")[0]
                logger.info(
                    "tenant_id derived from repo_owner=%s datasource=%s platform=%s",
                    tenant_id, datasource, platform,
                )
        except Exception:
            pass

    if not tenant_id:
        logger.warning(
            "no tenant_id in rule_config or payload for datasource=%s platform=%s; "
            "rejecting webhook", datasource, platform,
        )
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"No tenant_id configured or derivable for datasource={datasource!r}",
        )

    # ── Circuit breaker: fail-fast when Celery broker is down ────────────────
    # Returns 503 while the circuit is open so upstream load-balancers can
    # failover quickly instead of queueing requests that will time out anyway.
    from app.utils.circuit_breaker import get_breaker
    breaker = get_breaker()
    if not breaker.allow():
        logger.warning(
            "circuit breaker open — rejecting webhook datasource=%s platform=%s",
            datasource, platform,
        )
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Celery broker unavailable — circuit breaker open, retry later",
        )

    _t_dispatch = time.time()
    try:
        task = process_webhook_task.apply_async(
            kwargs={
                "platform":    platform,
                "datasource":  datasource,
                "raw_payload": raw_payload,
                "headers":     dict(request.headers),
                "received_at": received_at,
                "rule_config": rule_config,
                "tenant_id":   tenant_id,
            },
            queue="webhooks",
        )
        breaker.record_success()
    except Exception as _dispatch_exc:
        breaker.record_failure()
        logger.error(
            "task dispatch failed datasource=%s: %s", datasource, _dispatch_exc,
        )
        raise

    # ── fire-and-forget dispatch metrics ─────────────────────────────────────
    try:
        from app.monitoring import get_metrics
        m = get_metrics()
        if m:
            m.inc_webhook(datasource, platform, "accepted")
            m.observe_webhook_dispatch(datasource, platform, time.time() - _t_dispatch)
    except Exception:
        pass

    logger.info("webhook accepted task=%s platform=%s datasource=%s", task.id, platform, datasource)
    return WebhookResponse(task_id=task.id, message="accepted", received_at=received_at)
