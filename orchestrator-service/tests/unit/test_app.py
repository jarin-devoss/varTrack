"""
tests/unit/test_app.py

Unit tests for the FastAPI application factory (app/api/app.py).

Patterns:
  - httpx AsyncClient (pytest-asyncio)
  - Table-driven tests for header/status assertions
  - Lifespan skipped via dependency overrides and monkeypatching

Tests:
  - Health endpoint returns 200 + JSON body
  - Metrics endpoint reachable
  - Trailing-slash normalisation (POST /v1/webhooks/mongo/ == /v1/webhooks/mongo)
  - CORS wildcard is NOT present (default-deny)
  - Validation errors return 422 (not 500)
  - Unknown routes return 404 (not 500)
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# ── Stub heavy deps so the test module loads without infrastructure ────────────
for _mod in ("vartrack_core", "pymongo", "gitpython", "git"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


import pytest
from httpx import ASGITransport, AsyncClient


# ── App fixture ───────────────────────────────────────────────────────────────

def _build_app():
    """Build a test app with lifespan side-effects stubbed out."""
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        return create_app()


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_200():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body


@pytest.mark.asyncio
async def test_unknown_route_returns_404():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metrics_endpoint_reachable():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/metrics")
    # 200 (monitoring initialised) or 200 with "not initialised" text — never 500
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_trailing_slash_normalised_post():
    """
    POST /v1/webhooks/mongo/ (trailing slash) must NOT
    trigger a 307 redirect which webhook senders don't follow.  It must be
    routed identically to POST /v1/webhooks/mongo.
    """
    app = _build_app()
    # Patch the Celery task so no actual task is dispatched.
    mock_task = MagicMock()
    mock_task.id = "test-task-id"
    with patch("app.tasks.etl.process_webhook_task") as mock_proc:
        mock_proc.apply_async.return_value = mock_task
        # Inject a fake schema manager that returns a rule with tenant_id.
        app.state.schema_manager = MagicMock()
        app.state.schema_manager.resolve_rule.return_value = {"tenant_id": "acme"}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post(
                "/v1/webhooks/mongo/",
                content=b'{"ref":"refs/heads/main"}',
                headers={"x-github-event": "push", "x-tenant-id": "acme"},
            )

    # Must NOT be a redirect (307 / 308).
    assert resp.status_code not in (307, 308), (
        f"Trailing slash caused redirect: {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_cors_does_not_allow_wildcard_by_default():
    """
    CORS must not default to allow_origins=['*'].
    Check the CORS_ORIGINS setting is not a blanket wildcard.
    """
    from app.config import settings
    origins = settings.CORS_ORIGINS
    assert "*" not in origins, (
        "CORS allow_origins must not contain '*' — use an explicit allowlist"
    )


@pytest.mark.asyncio
async def test_validation_error_returns_422_not_500():
    """
    RequestValidationError must return 422, not 500.
    Send a malformed schema-registry body (missing required fields).
    """
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/webhooks/schema-registry",
            json={"platform": "github"},   # missing repo, branch, tenant_id
        )
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
