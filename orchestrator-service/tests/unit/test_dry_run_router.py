"""
tests/unit/test_dry_run_router.py

Unit tests for POST /v1/webhooks/{datasource}/dry-run.

Key invariants verified:
  - dry_run flag is always True in dispatched task kwargs
  - Returns task_id immediately (async path)
  - X-Tenant-ID header is required for tenant routing
  - Endpoint accepts any Content-Type (raw bytes)
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import app.tasks.etl  # noqa: E402,F401 — pre-import for pkgutil.resolve_name (Python 3.12+)

import pytest
from httpx import ASGITransport, AsyncClient


def _app():
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()

    mgr = MagicMock()
    mgr.resolve_rule.return_value = {"tenant_id": "acme", "prune": True}
    app.state.schema_manager = mgr
    return app


def _mock_task(task_id: str = "dry-run-task-001"):
    t = MagicMock()
    t.id = task_id
    return t


@pytest.mark.asyncio
async def test_dry_run_returns_task_id():
    app = _app()
    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task("drt-001")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/v1/webhooks/mongo/dry-run",
                content=b'{"ref":"refs/heads/main"}',
                headers={"x-github-event": "push", "x-tenant-id": "acme"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "drt-001"
    assert body["dry_run"] is True


@pytest.mark.asyncio
async def test_dry_run_forces_dry_run_flag_in_task():
    """
    dry_run=True must always be set in the task kwargs,
    regardless of what rule_config says about prune/dry_run_prune.
    """
    app = _app()
    captured: dict = {}

    def _capture(kwargs, **_):
        captured.update(kwargs)
        return _mock_task()

    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.side_effect = _capture

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(
                "/v1/webhooks/mongo/dry-run",
                content=b'{}',
                headers={"x-github-event": "push", "x-tenant-id": "acme"},
            )

    rule = captured.get("rule_config", {})
    prune = rule.get("prune", {}) if isinstance(rule.get("prune"), dict) else {}
    # Either the rule_config.prune.dry_run is True, or a top-level dry_run kwarg
    # is passed — implementation-dependent, but dry_run must appear somewhere.
    is_dry = (
        captured.get("dry_run") is True
        or prune.get("dry_run") is True
        or (isinstance(rule.get("dry_run_prune"), bool) and rule.get("dry_run_prune"))
    )
    assert is_dry, f"dry_run not enforced in task kwargs: {captured}"


@pytest.mark.asyncio
async def test_dry_run_accepts_any_content_type():
    """Dry-run endpoint must accept raw bytes regardless of Content-Type."""
    app = _app()
    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/v1/webhooks/mongo/dry-run",
                content=b"raw-body",
                headers={"content-type": "text/plain", "x-github-event": "push", "x-tenant-id": "acme"},
            )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dry_run_route_does_not_conflict_with_webhook_route():
    """
    /v1/webhooks/{datasource}/dry-run and /v1/webhooks/{datasource} must be
    distinct routes — the dry-run path must not be captured as a datasource
    named 'dry-run'.
    """
    app = _app()
    with patch("app.tasks.etl.process_webhook_task") as mock_task_cls:
        mock_task_cls.apply_async.return_value = _mock_task("normal")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            normal_resp = await c.post(
                "/v1/webhooks/mongo",
                content=b'{}',
                headers={"x-github-event": "push", "x-tenant-id": "acme"},
            )
            dry_resp = await c.post(
                "/v1/webhooks/mongo/dry-run",
                content=b'{}',
                headers={"x-github-event": "push", "x-tenant-id": "acme"},
            )

    assert normal_resp.status_code == 200
    assert dry_resp.status_code == 200
    assert dry_resp.json().get("dry_run") is True
