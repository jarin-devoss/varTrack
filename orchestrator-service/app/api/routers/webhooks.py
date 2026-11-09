"""
app/api/routers/webhooks.py
────────────────────────────
POST /v1/webhooks/{datasource}      →  process_webhook_task
POST /v1/webhooks/schema-registry   →  refresh_schema_task
"""
from __future__ import annotations

import logging
import time
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from app.api.deps import request_grant, route_push

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
    # Optional, and only ever a claim to check: the tenant is the one whose registered schema
    # repository `repo` is. A name that is not that tenant's is refused.
    tenant_id:   Optional[str] = None


@router.post("/webhooks/schema-registry", response_model=WebhookResponse)
async def schema_webhook(request: Request, body: SchemaWebhookBody) -> WebhookResponse:
    """Refresh schema registry when the schema repo receives a push."""
    from app import tenants
    from app.schema_registry.manager import get_schema_manager
    from app.tasks.etl import refresh_schema_task

    if body.tenant_id is not None:
        try:
            tenants.check_name(body.tenant_id)
        except tenants.InvalidTenant as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    mgr = getattr(request.app.state, "schema_manager", None) or get_schema_manager()
    tenant = mgr.tenant_for_repo(body.repo)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"{body.repo} is not a registered schema repository")
    if body.tenant_id is not None and body.tenant_id != tenant:
        raise HTTPException(status_code=403, detail=f"{body.repo} is not the schema repository of {body.tenant_id}")
    grant = request_grant(request)
    if grant is not None and grant.tenant and grant.tenant != tenant:
        raise HTTPException(status_code=403, detail="the grant was issued for another tenant")
    task = refresh_schema_task.apply_async(
        kwargs={
            "platform":    body.platform,
            "repo":        body.repo,
            "branch":      body.branch,
            "raw_payload": body.raw_payload,
            "headers":     dict(request.headers),
            "tenant_id":   tenant,
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

    # Which platform, tenant and rule the push is for: the pushed repository decides, and the dry run
    # of this route asks the same question (app.api.deps.route_push).
    route = route_push(request, datasource, raw_payload)
    platform, rule_config, tenant_id = route.platform, route.rule_config, route.tenant_id

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
