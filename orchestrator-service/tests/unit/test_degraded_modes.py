"""
A weaker mode exists only when an operator names it, is refused where it must be, and shows.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import dependencies, tls


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    dependencies.reset()
    monkeypatch.delenv("VARTRACK_ALLOW_DEGRADED", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    yield
    dependencies.reset()


def test_nothing_is_allowed_unless_named():
    assert not any(dependencies.allowed(m) for m in dependencies.MODES)


def test_a_named_mode_is_allowed_and_a_typo_is_an_error(monkeypatch):
    monkeypatch.setenv("VARTRACK_ALLOW_DEGRADED", "config, schema_validation")
    assert dependencies.allowed("config") and dependencies.allowed("schema_validation")
    assert not dependencies.allowed("tls")
    monkeypatch.setenv("VARTRACK_ALLOW_DEGRADED", "schema-validation")
    with pytest.raises(ValueError, match="schema-validation"):
        dependencies.allowed("config")


def test_the_modes_that_weaken_security_are_refused_in_production(monkeypatch):
    monkeypatch.setenv("VARTRACK_ALLOW_DEGRADED", "tls,nonce_store,config")
    monkeypatch.setenv("APP_ENV", "production")
    assert not dependencies.allowed("tls") and not dependencies.allowed("nonce_store")
    assert dependencies.allowed("config")


# ── TLS ───────────────────────────────────────────────────────────────────────

def _settings(**kw):
    base = dict(MTLS_ENABLED=True, APP_ENV="development")
    base.update(kw)
    return MagicMock(**base)


def test_no_certificate_stops_http_and_grpc_alike(monkeypatch):
    with patch("app.config.settings", _settings()), patch.object(tls, "_resolve_bundle", return_value=None):
        with pytest.raises(RuntimeError, match="VARTRACK_ALLOW_DEGRADED=tls"):
            tls.build_ssl_context()
        with pytest.raises(RuntimeError, match="insecure gRPC port"):
            tls.build_grpc_server_credentials()


def test_an_operator_who_named_tls_gets_plain_serving_and_it_shows(monkeypatch):
    monkeypatch.setenv("VARTRACK_ALLOW_DEGRADED", "tls")
    with patch("app.config.settings", _settings()), patch.object(tls, "_resolve_bundle", return_value=None):
        assert tls.build_ssl_context() is None
        assert tls.build_grpc_server_credentials() is None
    assert "tls" in dependencies.active_modes()


def test_production_never_serves_without_tls_even_when_named(monkeypatch):
    monkeypatch.setenv("VARTRACK_ALLOW_DEGRADED", "tls")
    monkeypatch.setenv("APP_ENV", "production")
    with patch("app.config.settings", _settings(APP_ENV="production")), \
            patch.object(tls, "_resolve_bundle", return_value=None):
        with pytest.raises(RuntimeError):
            tls.build_ssl_context()
        with pytest.raises(RuntimeError):
            tls.build_grpc_server_credentials()
    assert dependencies.active_modes() == {}


def test_tls_not_asked_for_is_not_a_degraded_mode():
    with patch("app.config.settings", _settings(MTLS_ENABLED=False)):
        assert tls.build_ssl_context() is None and tls.build_grpc_server_credentials() is None
    assert dependencies.active_modes() == {}


# ── the sync lease ────────────────────────────────────────────────────────────

def _broken_redis():
    redis = MagicMock()
    redis.lock.return_value.acquire.side_effect = ConnectionError("redis down")
    return redis


def test_a_lock_that_cannot_be_taken_stops_a_production_sync(monkeypatch):
    from app.pipeline import stage_sync

    monkeypatch.setenv("APP_ENV", "production")
    with patch("app.ui.ledger._redis", return_value=_broken_redis()):
        with pytest.raises(dependencies.LockUnavailable, match="cannot be taken"):
            with stage_sync.target_locks("acme", "redis", ["production"], {"destination_template": "{env}"}):
                pytest.fail("the sync ran without its lock")
    assert dependencies.snapshot()["redis"]["state"] == "unavailable"


def test_outside_production_a_missing_lock_is_said_and_the_sync_goes_on():
    from app.pipeline import stage_sync

    ran = []
    with patch("app.ui.ledger._redis", return_value=_broken_redis()):
        with stage_sync.target_locks("acme", "redis", ["production"], {"destination_template": "{env}"}):
            ran.append(True)
    assert ran == [True]


# ── readiness ─────────────────────────────────────────────────────────────────

def test_readiness_lists_every_dependency_and_every_mode():
    dependencies.up("git")
    dependencies.stale("schemas:demo", "refresh failed: network is unreachable")
    dependencies.mode_on("schema_validation", "the 'cue' program is not installed")
    out = dependencies.readiness()
    assert out["dependencies"]["git"]["state"] == "ok"
    assert out["dependencies"]["schemas:demo"]["state"] == "stale"
    assert out["degraded"] == {"schema_validation": "the 'cue' program is not installed"}
    assert "unavailable" not in out                      # a stale or failing tenant does not pull the service out


def test_an_unreadable_config_cue_makes_the_service_not_ready():
    dependencies.down("config.cue", "vartrack-bundle failed")
    assert dependencies.readiness()["unavailable"] == ["config.cue"]
    dependencies.up("config.cue")
    assert "unavailable" not in dependencies.readiness()


def test_an_operator_who_allowed_config_mode_is_ready_and_it_shows():
    dependencies.down("config.cue", "vartrack-bundle failed")
    dependencies.mode_on("config", "vartrack-bundle failed")
    out = dependencies.readiness()
    assert "unavailable" not in out and "config" in out["degraded"]
    dependencies.up("config.cue")                         # config.cue reads again: the mode is over
    assert dependencies.active_modes() == {}


@pytest.mark.asyncio
async def test_the_http_readiness_probe_says_why_it_is_not_ready():
    from httpx import ASGITransport, AsyncClient

    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()
    dependencies.down("config.cue", "vartrack-bundle failed: line 4")
    with patch("app.admin.server._check_broker", return_value=True), \
            patch("app.admin.server._check_redis_backend", return_value=True), \
            patch("app.admin.server._check_grpc_thread", return_value=True):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/v1/health/readiness")
    assert resp.status_code == 503
    body = resp.json()
    assert body["unavailable"] == ["config.cue"]
    assert "line 4" in body["dependencies"]["config.cue"]["reason"]


def test_the_dependency_gauge_follows_the_state():
    from prometheus_client import CollectorRegistry

    from app.monitoring.metrics import OrchestratorMetrics as Metrics

    metrics = Metrics.__new__(Metrics)
    from prometheus_client import Gauge
    registry = CollectorRegistry()
    metrics._dependency_up = Gauge("vartrack_dependency_up", "x", ["dependency"], registry=registry)
    with patch("app.monitoring.get_metrics", return_value=metrics):
        dependencies.down("git", "clone failed")
        assert registry.get_sample_value("vartrack_dependency_up", {"dependency": "git"}) == 0
        dependencies.stale("config.cue", "bad edit")
        assert registry.get_sample_value("vartrack_dependency_up", {"dependency": "config.cue"}) == 1
        dependencies.up("git")
        assert registry.get_sample_value("vartrack_dependency_up", {"dependency": "git"}) == 1
