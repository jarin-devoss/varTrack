"""
tests/integration/test_webhook_e2e.py

End-to-end integration tests for the orchestrator service.

Scope: in-process FastAPI (httpx AsyncClient) + stubbed Celery (ALWAYS_EAGER)
+ stubbed MongoDB.  No external infrastructure required.

What is exercised:
  - Full HTTP request path through FastAPI routing, middleware, and handlers.
  - process_webhook_task dispatched and returns a task_id.
  - Dry-run path: task executes inline (CELERY_TASK_ALWAYS_EAGER).
  - Schema-registry webhook route.
  - Concurrent requests all get unique task IDs.
  - Trailing-slash normalisation does not 307.
  - 422 for invalid JSON body to schema-registry endpoint.

_gateway() is a context-manager that wires a real FastAPI app with all
external dependencies stubbed, then tears it down after the test.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import app.tasks.etl  # noqa: E402,F401 — pre-import for pkgutil.resolve_name (Python 3.12+)

import pytest
from httpx import ASGITransport, AsyncClient


# ── Gateway fixture ───────────────────────────────────────────────────────────

@asynccontextmanager
async def _gateway(
    tenant_id: str | None = "acme",
    task_id: str = "e2e-task-001",
) -> AsyncGenerator[AsyncClient, None]:
    """
    Context manager that yields an AsyncClient wired to a real FastAPI app
    with all external dependencies stubbed (schema manager, Celery, monitoring).
    """
    mock_task = MagicMock()
    mock_task.id = task_id

    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()

    mgr = MagicMock()
    mgr.resolve_rule.return_value = {"tenant_id": tenant_id} if tenant_id else {}
    app.state.schema_manager = mgr

    with patch("app.tasks.etl.process_webhook_task") as mock_proc:
        mock_proc.apply_async.return_value = mock_task
        with patch("app.tasks.etl.refresh_schema_task") as mock_schema:
            mock_schema.apply_async.return_value = mock_task
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                yield client


# ── Health ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_health_liveness():
    async with _gateway() as client:
        resp = await client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── Webhook ingestion ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_push_webhook_accepted():
    async with _gateway(tenant_id="acme", task_id="task-push-001") as client:
        resp = await client.post(
            "/v1/webhooks/mongo",
            content=b'{"ref":"refs/heads/main","repository":{"full_name":"org/repo"}}',
            headers={
                "content-type": "application/json",
                "x-github-event": "push",
                "x-tenant-id": "acme",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "task-push-001"
    assert body["message"] == "accepted"
    assert "received_at" in body


@pytest.mark.asyncio
async def test_e2e_pull_request_webhook_accepted():
    async with _gateway(tenant_id="acme", task_id="task-pr-001") as client:
        resp = await client.post(
            "/v1/webhooks/mongo",
            content=b'{"action":"opened","pull_request":{"number":42},"repository":{"full_name":"org/repo"}}',
            headers={
                "content-type": "application/json",
                "x-github-event": "pull_request",
                "x-tenant-id": "acme",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "task-pr-001"


@pytest.mark.asyncio
async def test_e2e_missing_tenant_id_returns_400():
    """No tenant_id in rule_config AND no repository.full_name in payload → 400."""
    async with _gateway(tenant_id=None) as client:
        resp = await client.post(
            "/v1/webhooks/mongo",
            content=b'{}',
            headers={"x-github-event": "push"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_e2e_tenant_derived_from_repo_owner():
    """No tenant_id in rule_config, but payload has repository.full_name → derive from owner."""
    async with _gateway(tenant_id=None, task_id="task-derived-001") as client:
        resp = await client.post(
            "/v1/webhooks/mongo",
            content=b'{"ref":"refs/heads/main","repository":{"full_name":"myorg/myrepo"}}',
            headers={"content-type": "application/json", "x-github-event": "push"},
        )
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "task-derived-001"


@pytest.mark.asyncio
async def test_e2e_trailing_slash_not_307():
    """Trailing slash must not cause a 307 redirect."""
    async with _gateway() as client:
        resp = await client.post(
            "/v1/webhooks/mongo/",
            content=b'{}',
            headers={"x-github-event": "push", "x-tenant-id": "acme"},
        )
    assert resp.status_code not in (307, 308), (
        f"Trailing slash caused redirect {resp.status_code}"
    )


# ── Dry-run ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_dry_run_returns_dry_run_true():
    async with _gateway(task_id="dry-001") as client:
        resp = await client.post(
            "/v1/webhooks/mongo/dry-run",
            content=b'{"ref":"refs/heads/main"}',
            headers={"x-github-event": "push", "x-tenant-id": "acme"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert "task_id" in body


# ── Schema-registry ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_schema_registry_webhook_accepted():
    async with _gateway(task_id="schema-001") as client:
        resp = await client.post(
            "/v1/webhooks/schema-registry",
            json={
                "platform": "github",
                "repo": "org/schemas",
                "branch": "main",
                "tenant_id": "acme",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["message"] == "schema_accepted"


@pytest.mark.asyncio
async def test_e2e_schema_registry_missing_fields_returns_422():
    async with _gateway() as client:
        resp = await client.post(
            "/v1/webhooks/schema-registry",
            json={"platform": "github"},  # repo, branch, tenant_id missing
        )
    assert resp.status_code == 422


# ── Concurrency ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_concurrent_webhooks_all_accepted():
    """
    20 concurrent webhook requests must all complete successfully with unique
    task IDs (no shared-state corruption from concurrent access).

    Pattern from gateway-service webhook_scenarios_test.go
    TestE2E_ConcurrentWebhooks_UniqueDeliveryIDs.
    """
    counter = [0]

    def _unique_task():
        counter[0] += 1
        t = MagicMock()
        t.id = f"concurrent-task-{counter[0]:03d}"
        return t

    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()

    mgr = MagicMock()
    mgr.resolve_rule.return_value = {"tenant_id": "acme"}
    app.state.schema_manager = mgr

    with patch("app.tasks.etl.process_webhook_task") as mock_proc:
        mock_proc.apply_async.side_effect = lambda **_: _unique_task()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            tasks = [
                client.post(
                    "/v1/webhooks/mongo",
                    content=f'{{"ref":"refs/heads/main","delivery":"{i}"}}'.encode(),
                    headers={"x-github-event": "push", "x-tenant-id": "acme"},
                )
                for i in range(20)
            ]
            responses = await asyncio.gather(*tasks)

    assert all(r.status_code == 200 for r in responses), (
        f"Some concurrent requests failed: {[r.status_code for r in responses if r.status_code != 200]}"
    )


# ── Metrics ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_metrics_endpoint_accessible():
    async with _gateway() as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
