"""
tests/unit/test_internal_auth.py

The orchestrator's own front doors: signed grants (app/api/tokens.py), the HTTP
gate (app/api/middlewares/auth.py), the gRPC interceptor
(app/grpc_server/auth_interceptor.py), the CLI servicer's use of the verified
identity, the repository-scope check (app/pipeline/repo_scope.py) and the admin
listener.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import app.tasks.etl  # noqa: E402,F401  (so patch("app.tasks.etl.*") resolves)

import grpc
import pytest
from httpx import ASGITransport, AsyncClient

from app.api import tokens



SECRET = "0123456789abcdef0123456789abcdef"
OTHER_SECRET = "fedcba9876543210fedcba9876543210"

# Minted by libs/go/grants (TestInteropVector): secret SECRET, issued at
# 1_900_000_000, lifetime 300 s, audience "sync".
GO_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJ2YXJ0cmFjayIsImF1ZCI6InN5bmMiLCJzdWIiOiJhbGljZUBnaXRlYSIsImlhdCI6MTkwMDAwMDAwMCwi"
    "bmJmIjoxOTAwMDAwMDAwLCJleHAiOjE5MDAwMDAzMDAsInRlbmFudCI6ImRlbW8iLCJwbGF0Zm9ybSI6ImdpdGVhIiwi"
    "ZGF0YXNvdXJjZSI6Im1vbmdvIn0."
    "jU_NdTyOMJJOeVy0a1KFdi-SZfLaN3uLy6UdLkzTCOE"
)


@pytest.fixture
def auth_on(monkeypatch):
    """Internal authentication enforced with SECRET."""
    monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_SECRET", SECRET)
    monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_WATCH_SECRET", "")
    monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_ADMIN_SECRET", "")
    monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_INSECURE", False)


@pytest.fixture
def auth_off(monkeypatch):
    for name in ("INTERNAL_AUTH_SECRET", "INTERNAL_AUTH_WATCH_SECRET", "INTERNAL_AUTH_ADMIN_SECRET"):
        monkeypatch.setattr(f"app.config.settings.{name}", "")


def grant(audience="sync", **kw) -> str:
    return tokens.mint(audience, secret=kw.pop("secret", SECRET), **kw)


# ── tokens ────────────────────────────────────────────────────────────────────

class TestTokens:
    def test_roundtrip_carries_every_claim(self):
        tok = grant(subject="alice@gitea", tenant="demo", platform="gitea", datasource="mongo", now=1000)
        g = tokens.verify(tok, "sync", keys={"sync": SECRET}, now=1010)
        assert (g.audience, g.subject, g.tenant, g.platform, g.datasource) == (
            "sync", "alice@gitea", "demo", "gitea", "mongo")
        assert g.expires_at - g.issued_at == 60

    def test_a_token_minted_by_the_go_gateway_verifies(self):
        g = tokens.verify(GO_TOKEN, "sync", keys={"sync": SECRET}, now=1_900_000_100)
        assert (g.subject, g.tenant, g.platform, g.datasource) == ("alice@gitea", "demo", "gitea", "mongo")

    def test_wrong_audience_is_refused(self):
        tok = grant("watch", now=1000)
        with pytest.raises(tokens.GrantError, match="audience"):
            tokens.verify(tok, "sync", keys={"sync": SECRET}, now=1010)

    def test_wrong_key_is_refused(self):
        tok = grant(secret=OTHER_SECRET, now=1000)
        with pytest.raises(tokens.GrantError, match="signature"):
            tokens.verify(tok, "sync", keys={"sync": SECRET}, now=1010)

    def test_tampered_claims_are_refused(self):
        head, body, sig = grant(tenant="demo", now=1000).split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=="))
        claims["tenant"] = "someone-else"
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        with pytest.raises(tokens.GrantError, match="signature"):
            tokens.verify(f"{head}.{forged}.{sig}", "sync", keys={"sync": SECRET}, now=1010)

    def test_unsigned_and_foreign_algorithms_are_refused(self):
        body = grant(now=1000).split(".")[1]
        for alg in ("none", "HS384", "RS256"):
            head = base64.urlsafe_b64encode(json.dumps({"alg": alg, "typ": "JWT"}).encode()).rstrip(b"=").decode()
            for sig in ("", "AAAA"):
                with pytest.raises(tokens.GrantError):
                    tokens.verify(f"{head}.{body}.{sig}", "sync", keys={"sync": SECRET}, now=1010)

    def test_expired_and_not_yet_valid(self):
        tok = grant(ttl_seconds=60, now=10_000)
        with pytest.raises(tokens.GrantError, match="expired"):
            tokens.verify(tok, "sync", keys={"sync": SECRET}, now=10_000 + 60 + tokens.LEEWAY_SECONDS + 1)
        with pytest.raises(tokens.GrantError, match="not valid yet"):
            tokens.verify(tok, "sync", keys={"sync": SECRET}, now=10_000 - tokens.LEEWAY_SECONDS - 1)
        tokens.verify(tok, "sync", keys={"sync": SECRET}, now=10_000 + 60 + tokens.LEEWAY_SECONDS - 1)

    def test_lifetime_is_capped_per_audience(self):
        with pytest.raises(ValueError):
            tokens.mint("sync", secret=SECRET, ttl_seconds=tokens.MAX_LIFETIME_SECONDS["sync"] + 1)
        # a grant minted elsewhere with a longer life is refused on verification too
        head, body, _ = grant(now=1000).split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=="))
        claims["exp"] = claims["iat"] + 100_000
        long_body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        sig = tokens._signature(SECRET, f"{head}.{long_body}")
        with pytest.raises(tokens.GrantError, match="lifetime"):
            tokens.verify(f"{head}.{long_body}.{sig}", "sync", keys={"sync": SECRET}, now=1010)

    @pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "...", "not a token"])
    def test_malformed_input(self, bad):
        with pytest.raises(tokens.GrantError):
            tokens.verify(bad, "sync", keys={"sync": SECRET})

    def test_missing_time_claims_are_refused(self):
        head = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(json.dumps({"iss": "vartrack", "aud": "sync"}).encode()).rstrip(b"=").decode()
        sig = tokens._signature(SECRET, f"{head}.{body}")
        with pytest.raises(tokens.GrantError, match="iat, nbf and exp"):
            tokens.verify(f"{head}.{body}.{sig}", "sync", keys={"sync": SECRET})

    def test_no_key_for_an_audience(self):
        with pytest.raises(tokens.GrantError, match="no key"):
            tokens.verify(grant(now=1000), "sync", keys={}, now=1010)

    def test_bearer_parsing(self):
        assert tokens.bearer("Bearer abc.def.ghi") == "abc.def.ghi"
        assert tokens.bearer("bearer  x ") == "x"
        assert tokens.bearer("Basic abc") == ""
        assert tokens.bearer(None) == ""

    def test_keys_fall_back_to_the_main_secret(self, auth_on, monkeypatch):
        assert tokens.key_for("watch") == SECRET and tokens.key_for("admin") == SECRET
        monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_WATCH_SECRET", OTHER_SECRET)
        assert tokens.key_for("watch") == OTHER_SECRET and tokens.key_for("sync") == SECRET


class TestStartup:
    def test_production_refuses_to_start_without_a_secret(self, auth_off, monkeypatch):
        monkeypatch.setattr("app.config.settings.APP_ENV", "production")
        monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_INSECURE", False)
        with pytest.raises(RuntimeError, match="INTERNAL_AUTH_SECRET"):
            tokens.check_startup("test")

    def test_production_runs_open_only_when_told_to(self, auth_off, monkeypatch, caplog):
        monkeypatch.setattr("app.config.settings.APP_ENV", "production")
        monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_INSECURE", True)
        tokens.check_startup("test")
        assert "OFF" in caplog.text

    def test_development_runs_open_and_says_so(self, auth_off, monkeypatch, caplog):
        monkeypatch.setattr("app.config.settings.APP_ENV", "development")
        tokens.check_startup("test")
        assert "OFF" in caplog.text

    def test_a_short_secret_is_refused(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_SECRET", "short")
        with pytest.raises(RuntimeError, match="at least"):
            tokens.check_startup("test")

    def test_enforced_when_a_secret_is_set(self, auth_on):
        assert tokens.enforced()
        tokens.check_startup("test")

    def test_secret_file_must_be_readable(self, tmp_path, monkeypatch):
        from app.config import _secret_env
        f = tmp_path / "s"
        f.write_text(SECRET + "\n")
        monkeypatch.delenv("X_SECRET", raising=False)
        monkeypatch.setenv("X_SECRET_FILE", str(f))
        assert _secret_env("X_SECRET") == SECRET
        monkeypatch.setenv("X_SECRET_FILE", str(tmp_path / "missing"))
        with pytest.raises(RuntimeError, match="cannot be read"):
            _secret_env("X_SECRET")


# ── the HTTP gate ─────────────────────────────────────────────────────────────

class TestClassify:
    @pytest.mark.parametrize("method,path,want", [
        ("GET",  "/v1/health", "public"),
        ("GET",  "/v1/health/readiness", "public"),
        ("GET",  "/v1/auth/login", "public"),
        ("POST", "/v1/auth/logout", "public"),
        ("GET",  "/v1/ui/rules", "session"),
        ("POST", "/v1/ui/rules/gitea/mongo/sync", "session"),
        ("GET",  "/v1/tasks/abc", "either"),
        ("GET",  "/v1/tenants/demo/schemas", "either"),
        ("POST", "/v1/tenants/demo/schemas/warm", "grant"),
        ("POST", "/v1/tenants/demo/schemas", "grant"),
        ("POST", "/v1/webhooks/mongo", "grant"),
        ("POST", "/v1/webhooks/mongo/dry-run", "grant"),
        ("POST", "/v1/webhooks/schema-registry", "grant"),
        ("GET",  "/docs", "grant"),
        ("GET",  "/openapi.json", "grant"),
        ("GET",  "/v1/something-added-later", "grant"),
        ("DELETE", "/v1/tasks/abc", "grant"),
    ])
    def test_table(self, method, path, want):
        from app.api.middlewares.auth import classify
        assert classify(method, path) == want


def _app_settings():
    """The settings object app.api.app reads its CORS origins and environment from."""
    import importlib
    return importlib.import_module("app.api.app").settings


def _make_app(rule=None):
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()
    mgr = MagicMock()
    mgr.resolve_rule.return_value = {"tenant_id": "acme"} if rule is None else rule
    mgr.tenant_for_repo.return_value = "acme"
    mgr.list_tenant_ids.return_value = ["acme"]
    mgr.schema_commit.return_value = ""
    app.state.schema_manager = mgr
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


PUSH = b'{"ref":"refs/heads/main","repository":{"full_name":"org/repo"}}'


def _task(task_id="tid-1"):
    t = MagicMock()
    t.id = task_id
    return t


@pytest.mark.asyncio
class TestHttpGate:
    async def test_webhook_without_a_grant_is_refused(self, auth_on):
        app = _make_app()
        with patch("app.tasks.etl.process_webhook_task") as task:
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=PUSH, headers={"x-github-event": "push"})
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        task.apply_async.assert_not_called()

    async def test_webhook_with_a_forged_or_wrong_audience_grant_is_refused(self, auth_on):
        app = _make_app()
        with patch("app.tasks.etl.process_webhook_task") as task:
            async with _client(app) as c:
                for bad in (grant(secret=OTHER_SECRET), grant("watch"), "garbage"):
                    r = await c.post("/v1/webhooks/mongo", content=PUSH,
                                     headers={"authorization": f"Bearer {bad}", "x-github-event": "push"})
                    assert r.status_code == 401, bad
        task.apply_async.assert_not_called()

    async def test_webhook_with_a_grant_is_accepted(self, auth_on):
        app = _make_app()
        with patch("app.tasks.etl.process_webhook_task") as task:
            task.apply_async.return_value = _task("tid-ok")
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=PUSH, headers={
                    "authorization": f"Bearer {grant(tenant='acme')}", "x-github-event": "push"})
        assert r.status_code == 200 and r.json()["task_id"] == "tid-ok"

    async def test_the_grant_decides_tenant_and_platform_not_the_headers(self, auth_on):
        app = _make_app({"tenant_id": "acme"})
        with patch("app.tasks.etl.process_webhook_task") as task:
            task.apply_async.return_value = _task()
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=PUSH, headers={
                    "authorization": f"Bearer {grant(tenant='acme', platform='gitea')}",
                    "x-github-event": "push",          # would say "github"
                    "x-tenant-id": "victim"})          # would say another tenant
        assert r.status_code == 200
        app.state.schema_manager.resolve_rule.assert_called_once_with("gitea", "mongo", "acme")
        assert task.apply_async.call_args.kwargs["kwargs"]["platform"] == "gitea"

    async def test_a_grant_for_another_datasource_is_refused(self, auth_on):
        app = _make_app()
        with patch("app.tasks.etl.process_webhook_task") as task:
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/redis", content=PUSH, headers={
                    "authorization": f"Bearer {grant(datasource='mongo')}", "x-github-event": "push"})
        assert r.status_code == 403
        task.apply_async.assert_not_called()

    async def test_tenant_is_never_taken_from_the_payload_when_auth_is_on(self, auth_on):
        app = _make_app({})                    # the rule names no tenant
        with patch("app.tasks.etl.process_webhook_task") as task:
            task.apply_async.return_value = _task()
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=PUSH, headers={
                    "authorization": f"Bearer {grant()}", "x-github-event": "push"})
        assert r.status_code == 200
        assert task.apply_async.call_args.kwargs["kwargs"]["tenant_id"] == "default"   # not the owner "org"

    async def test_open_mode_keeps_the_old_behaviour(self, auth_off):
        app = _make_app({})
        with patch("app.tasks.etl.process_webhook_task") as task:
            task.apply_async.return_value = _task()
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=PUSH, headers={"x-github-event": "push"})
        assert r.status_code == 200            # the tenant is never the payload's owner ("org")

    async def test_a_payload_naming_an_uncovered_repository_is_refused(self, auth_on):
        app = _make_app({"tenant_id": "acme", "repositories": ["acme/configs"]})
        evil = b'{"ref":"refs/heads/main","repository":{"full_name":"evil/repo","clone_url":"http://h/evil/repo.git"}}'
        with patch("app.tasks.etl.process_webhook_task") as task:
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=evil, headers={
                    "authorization": f"Bearer {grant()}", "x-github-event": "push"})
        assert r.status_code == 403 and "not covered" in r.json()["detail"]
        task.apply_async.assert_not_called()

    async def test_a_payload_naming_an_excluded_repository_is_refused(self, auth_off):
        app = _make_app({"tenant_id": "acme", "repositories": ["acme/*"], "exclude_repositories": ["acme/private"]})
        body = b'{"ref":"refs/heads/main","repository":{"full_name":"acme/private"}}'
        with patch("app.tasks.etl.process_webhook_task") as task:
            async with _client(app) as c:
                r = await c.post("/v1/webhooks/mongo", content=body, headers={"x-github-event": "push"})
        assert r.status_code == 403 and "excluded" in r.json()["detail"]
        task.apply_async.assert_not_called()

    async def test_health_is_open_and_docs_are_not(self, auth_on):
        app = _make_app()
        async with _client(app) as c:
            assert (await c.get("/v1/health")).status_code == 200
            assert (await c.get("/v1/health/liveness")).status_code == 200
            assert (await c.get("/docs")).status_code == 401
            assert (await c.get("/openapi.json")).status_code == 401

    async def test_unknown_routes_need_a_grant_too(self, auth_on):
        app = _make_app()
        async with _client(app) as c:
            assert (await c.get("/v1/new-thing")).status_code == 401

    async def test_task_status_needs_a_grant_or_a_session(self, auth_on):
        app = _make_app()
        tid = "123e4567-e89b-12d3-a456-426614174000"
        fake = MagicMock()
        fake.AsyncResult.return_value = MagicMock(state="PENDING")
        with patch("app.worker.celery.celery", fake):
            async with _client(app) as c:
                assert (await c.get(f"/v1/tasks/{tid}")).status_code == 401
                ok = await c.get(f"/v1/tasks/{tid}", headers={"authorization": f"Bearer {grant()}"})
                assert ok.status_code == 200 and ok.json()["state"] == "PENDING"
                bad = await c.get(f"/v1/tasks/{tid}", headers={"authorization": "Bearer nope"})
                assert bad.status_code == 401

    async def test_task_status_stays_open_in_development(self, auth_off):
        app = _make_app()
        tid = "123e4567-e89b-12d3-a456-426614174000"
        fake = MagicMock()
        fake.AsyncResult.return_value = MagicMock(state="PENDING")
        with patch("app.worker.celery.celery", fake):
            async with _client(app) as c:
                assert (await c.get(f"/v1/tasks/{tid}")).status_code == 200

    async def test_task_status_needs_a_session_when_sign_in_is_on(self, auth_off):
        from app.ui import access
        app = _make_app()
        tid = "123e4567-e89b-12d3-a456-426614174000"
        fake = MagicMock()
        fake.AsyncResult.return_value = MagicMock(state="PENDING")
        with patch("app.worker.celery.celery", fake), \
             patch.object(access, "viewer", side_effect=access.Unauthenticated("sign in required")):
            async with _client(app) as c:
                assert (await c.get(f"/v1/tasks/{tid}")).status_code == 401

    async def test_with_sign_in_on_a_refusal_still_says_a_grant_would_do(self, auth_on):
        """Both doors are open (service grants and UI sessions): turning someone away says so, not just "sign in required"
        (which would tell a service that a signed grant is no use here)."""
        from app.ui import access
        app = _make_app()
        tid = "123e4567-e89b-12d3-a456-426614174000"
        fake = MagicMock()
        fake.AsyncResult.return_value = MagicMock(state="PENDING")
        with patch("app.worker.celery.celery", fake),              patch.object(access, "viewer", side_effect=access.Unauthenticated("sign in required")):
            async with _client(app) as c:
                refused = await c.get(f"/v1/tasks/{tid}")
                assert refused.status_code == 401
                assert refused.json()["detail"] == "a signed grant or a signed-in session is required"
                ok = await c.get(f"/v1/tasks/{tid}", headers={"authorization": f"Bearer {grant()}"})
                assert ok.status_code == 200

    async def test_schema_warm_needs_a_grant_and_stays_in_its_tenant(self, auth_on):
        app = _make_app()
        body = {"repo_url": "http://h/o/r.git", "branch": "main"}
        async with _client(app) as c:
            assert (await c.post("/v1/tenants/acme/schemas/warm", json=body)).status_code == 401
            other = await c.post("/v1/tenants/acme/schemas/warm", json=body,
                                 headers={"authorization": f"Bearer {grant(tenant='globex')}"})
            assert other.status_code == 403
            ok = await c.post("/v1/tenants/acme/schemas/warm", json=body,
                              headers={"authorization": f"Bearer {grant(tenant='acme')}"})
            assert ok.status_code == 200

    async def test_schema_list_needs_a_grant_or_a_session(self, auth_on):
        app = _make_app()
        async with _client(app) as c:
            assert (await c.get("/v1/tenants/acme/schemas")).status_code == 401
            ok = await c.get("/v1/tenants/acme/schemas", headers={"authorization": f"Bearer {grant()}"})
            assert ok.status_code == 200

    async def test_dry_run_needs_a_grant(self, auth_on):
        app = _make_app()
        async with _client(app) as c:
            r = await c.post("/v1/webhooks/mongo/dry-run", content=PUSH, headers={"x-github-event": "push"})
        assert r.status_code == 401

    async def test_schema_webhook_stays_in_the_grants_tenant(self, auth_on):
        app = _make_app()
        body = {"platform": "github", "repo": "http://h/o/r.git", "branch": "main"}
        with patch("app.tasks.etl.refresh_schema_task") as task:
            task.apply_async.return_value = _task("sch-1")
            async with _client(app) as c:
                other = await c.post("/v1/webhooks/schema-registry", json=body,
                                     headers={"authorization": f"Bearer {grant(tenant='globex')}"})
                assert other.status_code == 403
                ok = await c.post("/v1/webhooks/schema-registry", json=body,
                                  headers={"authorization": f"Bearer {grant(tenant='acme')}"})
                assert ok.status_code == 200

    async def test_cors_wildcard_is_dropped_when_auth_is_on(self, auth_on, monkeypatch):
        monkeypatch.setattr(_app_settings(), "CORS_ORIGINS", ["*"])
        app = _make_app()
        async with _client(app) as c:
            r = await c.options("/v1/health", headers={"origin": "http://evil.example",
                                                       "access-control-request-method": "GET"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

    async def test_cors_wildcard_still_works_in_development(self, auth_off, monkeypatch):
        monkeypatch.setattr(_app_settings(), "CORS_ORIGINS", ["*"])
        monkeypatch.setattr(_app_settings(), "APP_ENV", "development")
        app = _make_app()
        async with _client(app) as c:
            r = await c.options("/v1/health", headers={"origin": "http://dev.example",
                                                       "access-control-request-method": "GET"})
        assert r.headers.get("access-control-allow-origin") == "*"


# ── the gRPC interceptor ─────────────────────────────────────────────────────

class _Details:
    def __init__(self, method, metadata):
        self.method = method
        self.invocation_metadata = metadata


def _handler():
    seen = {}

    def behaviour(request, context):
        seen["grant"] = tokens.current_grant()
        return "served"

    return grpc.unary_unary_rpc_method_handler(behaviour), seen


def _call(interceptor, method, metadata):
    handler, seen = _handler()
    out = interceptor.intercept_service(lambda details: handler, _Details(method, metadata))
    return out, seen


class TestGrpcInterceptor:
    def test_audience_by_service(self):
        from app.grpc_server.auth_interceptor import audience_for
        assert audience_for("/vartrack.v1.Watcher/ListWatchTargets") == "watch"
        assert audience_for("/vartrack.v1.Orchestrator/ProcessWebhook") == "sync"
        assert audience_for("/vartrack.v1.CLIService/SyncFile") == "sync"
        assert audience_for("/unknown.Service/Method") == "sync"

    def test_off_in_development(self, auth_off):
        from app.grpc_server.auth_interceptor import GrantInterceptor
        handler, _ = _handler()
        assert GrantInterceptor().intercept_service(lambda d: handler, _Details("/x.Y/Z", ())) is handler

    def test_no_grant_is_refused_before_the_servicer_runs(self, auth_on):
        from app.grpc_server.auth_interceptor import GrantInterceptor
        out, seen = _call(GrantInterceptor(), "/vartrack.v1.CLIService/SyncFile", ())
        ctx = MagicMock()
        out.unary_unary(None, ctx)
        ctx.abort.assert_called_once()
        assert ctx.abort.call_args.args[0] == grpc.StatusCode.UNAUTHENTICATED
        assert "grant" not in seen

    def test_a_forged_user_header_alone_is_not_an_identity(self, auth_on):
        from app.grpc_server.auth_interceptor import GrantInterceptor
        out, seen = _call(GrantInterceptor(), "/vartrack.v1.CLIService/SyncFile",
                          (("x-cli-user", "alice-the-admin"), ("x-tenant-id", "victim")))
        ctx = MagicMock()
        out.unary_unary(None, ctx)
        assert ctx.abort.call_args.args[0] == grpc.StatusCode.UNAUTHENTICATED

    def test_a_grant_for_the_wrong_service_is_refused(self, auth_on):
        from app.grpc_server.auth_interceptor import GrantInterceptor
        out, _ = _call(GrantInterceptor(), "/vartrack.v1.Watcher/ListWatchTargets",
                       (("authorization", f"Bearer {grant('sync')}"),))
        ctx = MagicMock()
        out.unary_unary(None, ctx)
        assert ctx.abort.call_args.args[0] == grpc.StatusCode.UNAUTHENTICATED

    def test_a_valid_grant_reaches_the_servicer_and_is_visible_there_only(self, auth_on):
        from app.grpc_server.auth_interceptor import GrantInterceptor
        tok = grant(subject="alice@gitea", tenant="demo")
        out, seen = _call(GrantInterceptor(), "/vartrack.v1.CLIService/SyncFile",
                          (("authorization", f"Bearer {tok}"),))
        assert out.unary_unary(None, MagicMock()) == "served"
        assert seen["grant"].subject == "alice@gitea" and seen["grant"].tenant == "demo"
        assert tokens.current_grant() is None        # not left behind for the next call

    def test_the_watcher_grant_uses_its_own_key(self, auth_on, monkeypatch):
        from app.grpc_server.auth_interceptor import GrantInterceptor
        monkeypatch.setattr("app.config.settings.INTERNAL_AUTH_WATCH_SECRET", OTHER_SECRET)
        good = tokens.mint("watch", secret=OTHER_SECRET, subject="watcher")
        out, seen = _call(GrantInterceptor(), "/vartrack.v1.Watcher/ListWatchTargets",
                          (("authorization", f"Bearer {good}"),))
        assert out.unary_unary(None, MagicMock()) == "served" and seen["grant"].subject == "watcher"
        wrong_key = tokens.mint("watch", secret=SECRET, subject="watcher")
        out, _ = _call(GrantInterceptor(), "/vartrack.v1.Watcher/ListWatchTargets",
                       (("authorization", f"Bearer {wrong_key}"),))
        ctx = MagicMock()
        out.unary_unary(None, ctx)
        assert ctx.abort.call_args.args[0] == grpc.StatusCode.UNAUTHENTICATED


class TestCliServicerIdentity:
    def _set(self, g):
        return tokens.set_current_grant(g)

    def test_identity_comes_from_the_grant_not_from_metadata(self, auth_on):
        from app.grpc_server import cli_servicer as cs
        ctx = MagicMock()
        ctx.invocation_metadata.return_value = (("x-cli-user", "forged"), ("x-tenant-id", "victim"))
        marker = self._set(tokens.Grant("sync", "alice@gitea", tenant="demo"))
        try:
            assert cs._submitted_by(ctx) == "alice@gitea"
            assert cs._tenant_for(ctx, "") == "demo"
            assert cs._tenant_for(ctx, "demo") == "demo"
            cs._tenant_for(ctx, "victim")
            ctx.abort.assert_called_once()
            assert ctx.abort.call_args.args[0] == grpc.StatusCode.PERMISSION_DENIED
        finally:
            tokens.reset_current_grant(marker)

    def test_a_grant_without_a_tenant_is_refused_and_metadata_is_ignored(self, auth_on):
        from app.grpc_server import cli_servicer as cs
        ctx = MagicMock()
        ctx.invocation_metadata.return_value = (("x-cli-user", "forged"), ("x-tenant-id", "victim"))
        marker = self._set(tokens.Grant("sync", "svc"))
        try:
            assert cs._submitted_by(ctx) == "svc"
            cs._tenant_for(ctx, "victim")      # a grant the permission check did not issue names no tenant
            ctx.abort.assert_called_once()
            assert ctx.abort.call_args.args[0] == grpc.StatusCode.PERMISSION_DENIED
        finally:
            tokens.reset_current_grant(marker)

    def test_development_still_reads_the_metadata(self, auth_off):
        from app.grpc_server import cli_servicer as cs
        ctx = MagicMock()
        ctx.invocation_metadata.return_value = (("x-cli-user", "bob@gitea"), ("x-tenant-id", "acme"))
        assert cs._submitted_by(ctx) == "bob@gitea"
        assert cs._tenant_for(ctx, "") == "acme"
        assert cs._tenant_for(ctx, "explicit") == "explicit"


class TestProcessWebhookGrant:
    def _servicer(self, rule):
        from app.grpc_server.server import OrchestratorServicer, PROTO_AVAILABLE
        if not PROTO_AVAILABLE:
            pytest.skip("proto stubs not generated")
        return OrchestratorServicer(lambda platform, datasource: rule)

    def _request(self, platform="gitea", datasource="mongo", payload=PUSH.decode()):
        r = MagicMock()
        r.platform, r.datasource, r.raw_payload = platform, datasource, payload
        r.headers = {}
        r.received_at = None
        return r

    def test_a_grant_for_another_datasource_is_refused(self, auth_on):
        servicer = self._servicer({"tenant_id": "acme"})
        ctx = MagicMock()
        marker = tokens.set_current_grant(tokens.Grant("sync", "gateway", platform="gitea", datasource="redis"))
        try:
            with patch("app.tasks.etl.process_webhook_task") as task:
                servicer.ProcessWebhook(self._request(), ctx)
        finally:
            tokens.reset_current_grant(marker)
        ctx.set_code.assert_called_with(grpc.StatusCode.PERMISSION_DENIED)
        task.apply_async.assert_not_called()

    def test_an_uncovered_repository_is_refused(self, auth_on):
        servicer = self._servicer({"tenant_id": "acme", "repositories": ["acme/configs"]})
        ctx = MagicMock()
        marker = tokens.set_current_grant(tokens.Grant("sync", "gateway", platform="gitea", datasource="mongo"))
        try:
            with patch("app.tasks.etl.process_webhook_task") as task:
                servicer.ProcessWebhook(self._request(), ctx)      # PUSH names org/repo
        finally:
            tokens.reset_current_grant(marker)
        ctx.set_code.assert_called_with(grpc.StatusCode.PERMISSION_DENIED)
        task.apply_async.assert_not_called()

    def test_a_covered_repository_is_queued(self, auth_on):
        servicer = self._servicer({"tenant_id": "acme", "repositories": ["org/*"]})
        ctx = MagicMock()
        marker = tokens.set_current_grant(tokens.Grant("sync", "gateway", platform="gitea", datasource="mongo"))
        try:
            with patch("app.tasks.etl.process_webhook_task") as task:
                task.apply_async.return_value = _task("g-1")
                servicer.ProcessWebhook(self._request(), ctx)
        finally:
            tokens.reset_current_grant(marker)
        task.apply_async.assert_called_once()


# ── repository scope ─────────────────────────────────────────────────────────

class TestRepoScope:
    from app.pipeline import repo_scope as rs

    def test_names_from_every_spelling(self):
        rs = self.rs
        payload = {"repository": {"full_name": "acme/configs", "clone_url": "http://gitea:3000/acme/configs.git"},
                   "project": {"path_with_namespace": "acme/configs", "git_http_url": "https://gl/acme/configs.git"}}
        assert rs.pushed_repositories(payload) == ["acme/configs"]
        assert rs.pushed_repositories({"repository": {"full_name": "http://gitea:3000/acme/configs.git"}}) == ["acme/configs"]
        assert rs.pushed_repositories({}) == [] and rs.pushed_repositories("x") == []

    def test_rule_without_any_repository_covers_everything(self):
        self.rs.check_repository({"datasource": "mongo"}, {"repository": {"full_name": "any/thing"}})

    def test_repo_url_stands_in_for_repositories(self):
        rule = {"repo_url": "http://gitea:3000/acme/configs.git"}
        self.rs.check_repository(rule, {"repository": {"full_name": "acme/configs"}})
        with pytest.raises(self.rs.RepositoryNotAllowed):
            self.rs.check_repository(rule, {"repository": {"full_name": "evil/configs"}})

    def test_patterns_and_exclusions(self):
        rule = {"repositories": ["acme/*"], "exclude_repositories": ["acme/private*"]}
        self.rs.check_repository(rule, {"repository": {"full_name": "acme/configs"}})
        for bad in ("acme/private", "acme/private-x", "other/configs"):
            with pytest.raises(self.rs.RepositoryNotAllowed):
                self.rs.check_repository(rule, {"repository": {"full_name": bad}})

    def test_match_is_case_insensitive(self):
        self.rs.check_repository({"repositories": ["Acme/Configs"]}, {"repository": {"full_name": "acme/configs"}})

    def test_disagreeing_spellings_cannot_smuggle_a_repository(self):
        rule = {"repositories": ["acme/configs"]}
        sneaky = {"repository": {"full_name": "acme/configs", "clone_url": "http://h/evil/repo.git"}}
        with pytest.raises(self.rs.RepositoryNotAllowed):
            self.rs.check_repository(rule, sneaky)

    def test_a_payload_that_names_nothing_is_refused_when_the_rule_names_something(self):
        with pytest.raises(self.rs.RepositoryNotAllowed, match="does not name"):
            self.rs.check_repository({"repositories": ["acme/configs"]}, {"ref": "refs/heads/main"})

    def test_per_repository_overrides_extend_the_rule(self):
        rule = {"repositories": ["acme/a"], "overrides": [{"enable": True, "match_repositories": ["acme/b"]}]}
        self.rs.check_repository(rule, {"repository": {"full_name": "acme/b"}})

    def test_the_permission_engine_is_told_the_same_patterns(self):
        from app import authz_client
        rule = {"platform": "gitea", "datasource": "mongo", "repositories": ["acme/*"],
                "exclude_repositories": ["acme/x"], "repo_url": "http://h/o/r.git"}
        facts = authz_client.rule_facts(rule)
        assert (list(facts.repositories), list(facts.exclude_repositories)) == self.rs.patterns(rule)

    def test_the_payload_stage_refuses_an_uncovered_repository(self):
        from app.pipeline.stage_payload import run_stage_payload
        rule = {"platform": "github", "datasource": "mongo", "repositories": ["acme/configs"]}
        ok = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "acme/configs",
                                                                    "clone_url": "http://h/acme/configs.git"}})
        assert run_stage_payload(ok, "github", rule, "mongo", "acme").repo_url == "http://h/acme/configs.git"
        evil = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "evil/x",
                                                                      "clone_url": "http://h/evil/x.git"}})
        with pytest.raises(self.rs.RepositoryNotAllowed):
            run_stage_payload(evil, "github", rule, "mongo", "acme")

    def test_the_task_reports_the_refusal(self):
        from app.tasks import etl
        rule = {"platform": "github", "datasource": "mongo", "repositories": ["acme/configs"], "tenant_id": "acme"}
        evil = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "evil/x"}})
        with patch.object(etl.process_webhook_task, "update_state"), \
             patch("app.monitoring.tracer.start_webhook_span"):
            out = etl.process_webhook_task.run(
                platform="github", datasource="mongo", raw_payload=evil, headers={},
                received_at=0.0, rule_config=rule, tenant_id="acme")
        assert out["state"] == "FAILED" and "not covered" in out["message"]


# ── the admin listener ───────────────────────────────────────────────────────

class TestAdminListener:
    def _serve(self):
        from app.admin.server import AdminServer
        srv = AdminServer(host="127.0.0.1", port=0)
        srv.start()
        return srv, srv._server.server_address[1]

    @staticmethod
    def _get(port, path, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_default_bind_is_loopback(self):
        from app.admin.server import AdminServer
        assert AdminServer().__dict__["_host"] == "127.0.0.1"
        from app.config import _Settings
        assert _Settings.ADMIN_HOST in ("127.0.0.1", __import__("os").environ.get("ADMIN_HOST", "127.0.0.1"))

    def test_probes_open_everything_else_needs_an_admin_grant(self, auth_on):
        srv, port = self._serve()
        try:
            assert self._get(port, "/healthz") == 200
            assert self._get(port, "/health/liveness") == 200
            for path in ("/metrics", "/circuit-breaker", "/debug/pprof/goroutine"):
                assert self._get(port, path) == 401, path
                assert self._get(port, path, grant("sync")) == 401, path     # wrong audience
                assert self._get(port, path, "junk") == 401, path
            assert self._get(port, "/circuit-breaker", grant("admin", ttl_seconds=300)) == 200
        finally:
            srv.stop()

    def test_open_in_development(self, auth_off):
        srv, port = self._serve()
        try:
            assert self._get(port, "/circuit-breaker") == 200
        finally:
            srv.stop()
