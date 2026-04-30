"""
tests/unit/test_webhooks_router.py

Unit tests for POST /v1/webhooks/{datasource} and
POST /v1/webhooks/schema-registry.

Patterns:
  - httpx AsyncClient with ASGITransport (no real server needed)
  - Celery task stubbed via unittest.mock.patch
  - Table-driven pytest.mark.parametrize for platform detection cases
  - Schema manager stubbed on app.state so no I/O is required
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

# Pre-import so patch("app.tasks.etl.*") can resolve via pkgutil in Python 3.12+.
import app.tasks.etl  # noqa: E402,F401

import pytest
from httpx import ASGITransport, AsyncClient


# ── App factory helper ────────────────────────────────────────────────────────

def _app_with_tenant(tenant_id: str | None = "acme"):
    """Create app with a fake schema manager that returns a rule with tenant_id."""
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()

    rule = {"tenant_id": tenant_id} if tenant_id else {}
    mgr = MagicMock()
    mgr.resolve_rule.return_value = rule
    app.state.schema_manager = mgr
    return app


def _mock_task(task_id: str = "task-abc-123"):
    t = MagicMock()
    t.id = task_id
    return t


# ── POST /v1/webhooks/{datasource} ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_accepted_returns_202_with_task_id():
    app = _app_with_tenant("acme")
    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task("tid-001")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
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
    assert body["task_id"] == "tid-001"
    assert body["message"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_missing_tenant_id_returns_400():
    """
    When no tenant_id is in rule_config AND payload has no repository.full_name,
    the endpoint must reject with 400.
    """
    app = _app_with_tenant(tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/v1/webhooks/mongo",
            content=b'{"ref":"refs/heads/main"}',
            headers={"x-github-event": "push"},
        )
    assert resp.status_code == 400
    assert "tenant_id" in resp.text


@pytest.mark.asyncio
async def test_webhook_tenant_derived_from_repo_owner():
    """
    When rule_config has no tenant_id, derive it from repository.full_name
    in the webhook payload (topLevelOrg pattern).
    """
    app = _app_with_tenant(tenant_id=None)
    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task("tid-derived")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/v1/webhooks/mongo",
                content=b'{"ref":"refs/heads/main","repository":{"full_name":"myorg/myrepo"}}',
                headers={"content-type": "application/json", "x-github-event": "push"},
            )

    assert resp.status_code == 200
    call_kwargs = mock_task_cls.apply_async.call_args.kwargs["kwargs"]
    assert call_kwargs["tenant_id"] == "myorg"


@pytest.mark.parametrize("header,expected_platform", [
    ("x-github-event",    "github"),
    ("x-gitlab-event",    "gitlab"),
    ("x-bitbucket-event", "bitbucket"),
])
@pytest.mark.asyncio
async def test_platform_detected_from_header(header: str, expected_platform: str):
    """Platform is inferred from the event header — no explicit platform param needed."""
    app = _app_with_tenant("acme")
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _mock_task()

    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.side_effect = lambda kwargs, **_: _capture(**kwargs) or _mock_task()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(
                "/v1/webhooks/mongo",
                content=b'{}',
                headers={header: "push", "x-tenant-id": "acme"},
            )

    assert captured.get("platform") == expected_platform, (
        f"header {header!r} should yield platform {expected_platform!r}"
    )


@pytest.mark.asyncio
async def test_task_enqueued_to_webhooks_queue():
    """process_webhook_task must be dispatched to the 'webhooks' queue."""
    app = _app_with_tenant("acme")
    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(
                "/v1/webhooks/mongo",
                content=b'{}',
                headers={"x-github-event": "push", "x-tenant-id": "acme"},
            )

    call_kwargs = mock_task_cls.apply_async.call_args
    assert call_kwargs.kwargs.get("queue") == "webhooks"


# ── POST /v1/webhooks/schema-registry ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_schema_webhook_accepted():
    app = _app_with_tenant("acme")
    with patch("app.tasks.etl.refresh_schema_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task("schema-task-001")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/v1/webhooks/schema-registry",
                json={
                    "platform": "github",
                    "repo": "org/schemas",
                    "branch": "main",
                    "tenant_id": "acme",
                },
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "schema-task-001"
    assert body["message"] == "schema_accepted"


@pytest.mark.asyncio
async def test_schema_webhook_enqueued_to_schema_queue():
    app = _app_with_tenant("acme")
    with patch("app.tasks.etl.refresh_schema_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(
                "/v1/webhooks/schema-registry",
                json={"platform": "github", "repo": "org/s", "branch": "main", "tenant_id": "t"},
            )

    call_kwargs = mock_task_cls.apply_async.call_args
    assert call_kwargs.kwargs.get("queue") == "schema"


@pytest.mark.asyncio
async def test_schema_webhook_missing_field_returns_422():
    app = _app_with_tenant("acme")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/v1/webhooks/schema-registry",
            json={"platform": "github"},  # repo, branch, tenant_id missing
        )
    assert resp.status_code == 422
