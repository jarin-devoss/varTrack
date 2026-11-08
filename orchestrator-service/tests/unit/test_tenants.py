"""
A tenant is something an operator declared; a request only ever names one.

What a request carries is checked, never trusted: the name has one shape wherever it enters,
a schema push belongs to the tenant whose registered schema repository it came from (and
re-reads that registered repository, not the pushed one), and no file chooses the tenant a
rule acts for.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import cli_ops, tenants

REGISTERED = {
    "demo":  {"repo_url": "http://gitea:3000/demo-org/schemas.git", "branch": "main", "token": "demo-token"},
    "EVIL":  {"repo_url": "http://gitea:3000/evil-org/schemas.git", "branch": "main", "token": "evil-token"},
}


# ── C7: one shape for a tenant name ───────────────────────────────────────────

@pytest.mark.parametrize("name", ["demo", "default", "Acme_Corp", "team-1", "a", "9lives", "x" * 63])
def test_a_tenant_name_is_letters_digits_dash_and_underscore(name):
    assert tenants.valid_name(name) and tenants.check_name(name) == name


@pytest.mark.parametrize("name", [
    "demo&limit=1", "a b", "../x", "x/y", "x?y", "x#y", "x:y", "-lead", "_lead", "", " demo", "é", "x" * 64,
    "demo\n", "a\x00b", None, 5,
])
def test_anything_else_is_not_a_tenant_name(name):
    assert not tenants.valid_name(name)
    with pytest.raises(tenants.InvalidTenant, match="is not a tenant name"):
        tenants.check_name(name)


def test_the_message_never_repeats_a_whole_long_value():
    with pytest.raises(tenants.InvalidTenant) as caught:
        tenants.check_name("x&" + "y" * 5000)
    assert len(str(caught.value)) < 250


def test_a_tenant_resolved_for_the_cli_must_be_a_name():
    assert cli_ops.resolve_tenant(None, "  ", "acme") == "acme"
    assert cli_ops.resolve_tenant("", None) == "default"
    with pytest.raises(tenants.InvalidTenant):
        cli_ops.resolve_tenant("demo&limit=1")


def test_the_cli_servicer_answers_invalid_argument_for_a_bad_tenant():
    import grpc
    from app.grpc_server import cli_servicer

    class Aborted(Exception):
        pass

    class Context:
        code = None

        def invocation_metadata(self):
            return []

        def abort(self, code, details):
            self.code = code
            raise Aborted(details)

    ctx = Context()
    with pytest.raises(Aborted):
        cli_servicer._tenant_for(ctx, "demo&limit=1")
    assert ctx.code == grpc.StatusCode.INVALID_ARGUMENT


def _app(tenant_for_repo, listed=("demo",), env=None):
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()
    mgr = MagicMock()
    mgr.tenant_for_repo.side_effect = tenant_for_repo
    mgr.list_tenant_ids.return_value = list(listed)
    mgr.get_all_rules.return_value = []
    mgr.schema_commit.return_value = ""
    app.state.schema_manager = mgr
    return app, mgr


def _by_repo(repo):
    return {"demo-org/schemas": "demo", "evil-org/schemas": "EVIL"}.get(repo)


@pytest.mark.asyncio
async def test_the_http_routes_refuse_a_tenant_that_is_not_a_name():
    app, _ = _app(_by_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/v1/tenants/a%26b/schemas/warm")).status_code == 400
        assert (await c.get("/v1/tenants/a%20b/schemas")).status_code == 400
        resp = await c.post("/v1/webhooks/schema-registry", json={
            "platform": "gitea", "repo": "demo-org/schemas", "branch": "main", "tenant_id": "a&b"})
        assert resp.status_code == 400


# ── P19: a schema push belongs to the tenant whose repository it came from ────

@pytest.fixture
def mgr():
    from app.schema_registry.manager import SchemaRegistryManager
    manager = SchemaRegistryManager.__new__(SchemaRegistryManager)
    import threading
    manager._tenant_configs = {k: dict(v) for k, v in REGISTERED.items()}
    manager._lock = threading.Lock()
    return manager


def test_the_tenant_of_a_schema_repository_is_the_one_it_was_registered_for(mgr):
    assert mgr.tenant_for_repo("demo-org/schemas") == "demo"
    assert mgr.tenant_for_repo("http://gitea:3000/evil-org/schemas.git") == "EVIL"
    assert mgr.tenant_for_repo("DEMO-ORG/Schemas") == "demo"
    assert mgr.tenant_for_repo("demo-org/other") is None
    assert mgr.tenant_for_repo("") is None
    assert mgr.registered_branch("demo") == "main" and mgr.registered_branch("nobody") == "main"


def test_a_tenant_cannot_be_registered_or_pointed_elsewhere_by_a_request(mgr):
    from app.schema_registry.manager import SchemaRegistryManager
    assert not hasattr(SchemaRegistryManager, "register_tenant")
    import inspect
    assert list(inspect.signature(SchemaRegistryManager.get_or_clone).parameters) == ["self", "tenant_id"]


def _refresh(manager, **kw):
    from app.tasks.etl import refresh_schema_task
    kwargs = dict(platform="gitea", repo="demo-org/schemas", branch="main", raw_payload="{}", headers={})
    kwargs.update(kw)
    with patch("app.schema_registry.manager.get_schema_manager", return_value=manager):
        return refresh_schema_task.run(**kwargs)


def test_a_push_to_a_registered_schema_repository_refreshes_that_tenant_only(tmp_path):
    manager = MagicMock()
    manager.tenant_for_repo.return_value = "demo"
    manager.registered_branch.return_value = "main"
    manager.get_or_clone.return_value = tmp_path
    (tmp_path / "a.cue").write_text("x: 1")
    result = _refresh(manager)
    assert result["state"] == "SUCCEEDED" and result["tenant_id"] == "demo" and result["cue_files"] == 1
    manager.invalidate.assert_called_once_with("demo")
    manager.get_or_clone.assert_called_once_with("demo")   # the registered repository, nothing from the push


def test_a_push_from_a_repository_nobody_registered_changes_nothing(caplog):
    manager = MagicMock()
    manager.tenant_for_repo.return_value = None
    with caplog.at_level(logging.WARNING):
        result = _refresh(manager, repo="attacker/schemas",
                          headers={"Authorization": "Bearer stolen-or-chosen"})
    assert result["state"] == "FAILED" and result["message"] == "schema_repository_not_registered"
    manager.invalidate.assert_not_called()
    manager.get_or_clone.assert_not_called()
    assert "stolen-or-chosen" not in caplog.text


def test_a_push_cannot_be_claimed_for_another_tenant():
    manager = MagicMock()
    manager.tenant_for_repo.return_value = "EVIL"
    manager.registered_branch.return_value = "main"
    result = _refresh(manager, repo="evil-org/schemas", tenant_id="demo")
    assert result["state"] == "FAILED" and "another_tenant" in result["message"]
    manager.get_or_clone.assert_not_called()


def test_a_push_to_a_branch_the_registry_does_not_follow_is_ignored():
    manager = MagicMock()
    manager.tenant_for_repo.return_value = "demo"
    manager.registered_branch.return_value = "main"
    result = _refresh(manager, branch="experiment")
    assert result["state"] == "SUCCEEDED" and result["message"] == "schema_branch_not_followed"
    manager.get_or_clone.assert_not_called()


def test_the_webhooks_headers_are_never_a_credential():
    from app.tasks import payload
    assert not hasattr(payload, "token_from_headers")


@pytest.mark.asyncio
async def test_the_http_schema_webhook_derives_the_tenant_from_the_repository():
    app, _ = _app(_by_repo)
    with patch("app.tasks.etl.refresh_schema_task") as task:
        task.apply_async.return_value = MagicMock(id="t-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            ok = await c.post("/v1/webhooks/schema-registry",
                              json={"platform": "gitea", "repo": "evil-org/schemas", "branch": "main"})
            assert ok.status_code == 200
            assert task.apply_async.call_args.kwargs["kwargs"]["tenant_id"] == "EVIL"

            unknown = await c.post("/v1/webhooks/schema-registry",
                                   json={"platform": "gitea", "repo": "attacker/schemas", "branch": "main"})
            assert unknown.status_code == 404

            claimed = await c.post("/v1/webhooks/schema-registry", json={
                "platform": "gitea", "repo": "evil-org/schemas", "branch": "main", "tenant_id": "demo"})
            assert claimed.status_code == 403
    assert task.apply_async.call_count == 1


@pytest.mark.asyncio
async def test_warm_names_no_repository_and_refuses_an_unregistered_tenant():
    app, mgr = _app(_by_repo, listed=("demo",))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # What the body says is not read: there is nothing in it to choose a repository with.
        ok = await c.post("/v1/tenants/demo/schemas/warm",
                          json={"repo_url": "http://attacker/x.git", "branch": "x", "token": "t"})
        assert ok.status_code == 200
        mgr.get_or_clone.assert_called_once_with("demo")
        assert (await c.post("/v1/tenants/nobody/schemas/warm")).status_code == 404


# ── tenant_id is varTrack's, not a file's ─────────────────────────────────────

def test_a_bundle_json_cannot_name_its_own_tenant(monkeypatch, caplog):
    from app.pipeline import datasource_settings as ds
    from app.pipeline import rule_layers
    cue = {"platforms": [{"gitea": {"endpoint": "http://gitea:3000"}}],
           "rules": [{"platform": "gitea", "datasource": "mongo", "repositories": ["acme/configs"],
                      "file_name": "app.yaml"}]}
    monkeypatch.setattr(ds, "_bundle", lambda: cue)
    rule_layers._warned.clear()
    with caplog.at_level(logging.WARNING):
        rule = rule_layers.layered_rule(
            cue["rules"][0], {"platform": "gitea", "datasource": "mongo", "tenant_id": "demo"}, "EVIL", cue)
    assert rule["tenant_id"] == "EVIL"
    assert "tenant_id" in caplog.text and "ignored" in caplog.text


def test_tenant_id_is_not_an_option_a_file_may_set():
    from app.pipeline import rule_options
    assert "tenant_id" not in rule_options.FIELDS and "tenant_id" not in rule_options.TENANT_SETTABLE


# ── Phase 2: tenants the operator declared, and the tenant of a push ──────────

DECLARED = [
    {"name": "payments", "repositories": ["pay-org/*"],
     "schema_repo": {"platform": "gitea", "repo": "pay-org/schemas", "branch": "main", "token": {"value": "pay-token"}}},
    {"name": "search", "repositories": ["search-org/app", "search-org/infra"]},
    {"name": "bare"},
]


@pytest.fixture
def declared(monkeypatch):
    monkeypatch.setattr(tenants, "declared", lambda: [dict(t) for t in DECLARED])


def test_with_no_tenants_declared_a_push_is_the_rules_tenant(monkeypatch):
    monkeypatch.setattr(tenants, "declared", lambda: [])
    assert tenants.tenant_of_push({"tenant_id": "demo"}, ["any/thing"]) == "demo"
    assert tenants.tenant_of_push({}, ["any/thing"]) == "default"
    assert tenants.is_declared("whatever") and not tenants.is_declared("what ever")


def test_a_push_belongs_to_the_tenant_whose_repositories_it_came_from(declared):
    assert tenants.tenant_of_push({}, ["pay-org/api"]) == "payments"
    assert tenants.tenant_of_push({}, ["Search-Org/App"]) == "search"
    assert tenants.is_declared("payments") and not tenants.is_declared("EVIL")


def test_a_repository_nobody_owns_belongs_to_nobody(declared):
    assert tenants.tenant_of_push({}, ["evil-org/x"]) is None
    assert tenants.tenant_of_push({}, []) is None


def test_every_spelling_a_payload_gives_must_belong_to_the_same_tenant(declared):
    assert tenants.tenant_of_push({}, ["pay-org/api", "pay-org/api"]) == "payments"
    assert tenants.tenant_of_push({}, ["pay-org/api", "search-org/app"]) is None


def test_a_rule_narrows_the_tenants_a_push_may_belong_to(declared):
    assert tenants.tenant_of_push({"tenants": ["payments"]}, ["pay-org/api"]) == "payments"
    # a tenant the rule leaves out does not get the push, even though its repository is its own
    assert tenants.tenant_of_push({"tenants": ["search"]}, ["pay-org/api"]) is None
    assert tenants.tenant_of_push({"tenants": ["search", "bare"]}, ["pay-org/api"]) is None


def test_a_repository_no_tenant_covers_goes_to_the_rules_only_tenant(declared):
    assert tenants.tenant_of_push({"tenants": ["bare"]}, ["unlisted/repo"]) == "bare"
    assert tenants.tenant_of_push({"tenants": ["bare", "search"]}, ["unlisted/repo"]) is None
    assert tenants.tenant_of_push({"tenants": ["gone"]}, ["unlisted/repo"]) is None
    assert tenants.tenant_of_push({}, ["unlisted/repo"]) is None


def _manager(monkeypatch, env=None):
    import os
    import threading
    from app.schema_registry import manager as m
    for key in list(os.environ):
        if key.startswith("SCHEMA_TENANT_"):
            monkeypatch.delenv(key)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: {"tenants": DECLARED})
    monkeypatch.setattr("app.pipeline.presets._platform", lambda name: ("http://git.example", None))
    mgr = m.SchemaRegistryManager.__new__(m.SchemaRegistryManager)
    mgr._entries, mgr._lock, mgr._tenant_configs, mgr._from_bundle = {}, threading.Lock(), {}, set()
    mgr._load_env_configs()
    return mgr


def test_a_declared_tenants_schema_repository_is_registered_from_config_cue(monkeypatch):
    mgr = _manager(monkeypatch)
    assert mgr.list_tenant_ids() == ["payments"]
    assert mgr.tenant_for_repo("pay-org/schemas") == "payments"
    reg = mgr.registration("payments")
    assert reg["repo_url"] == "http://git.example/pay-org/schemas.git"
    assert reg["branch"] == "main" and reg["token"] == "pay-token"


def test_a_tenant_taken_out_of_config_cue_is_unregistered(monkeypatch):
    mgr = _manager(monkeypatch)
    assert mgr.tenant_for_repo("pay-org/schemas") == "payments"
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: {"tenants": []})
    assert mgr.tenant_for_repo("pay-org/schemas") is None and mgr.list_tenant_ids() == []


def test_the_env_registration_still_works_for_one_release_and_says_so(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        mgr = _manager(monkeypatch, {"SCHEMA_TENANT_OLD_REPO": "http://gitea:3000/old-org/schemas.git"})
    assert "deprecated" in caplog.text and "SCHEMA_TENANT_OLD" in caplog.text
    assert mgr.tenant_for_repo("old-org/schemas") == "old"
    assert sorted(mgr.list_tenant_ids()) == ["old", "payments"]


def test_a_declared_schema_repository_wins_over_an_env_var_of_the_same_tenant(monkeypatch):
    mgr = _manager(monkeypatch, {"SCHEMA_TENANT_PAYMENTS_REPO": "http://evil/x.git"})
    assert mgr.registration("payments")["repo_url"] == "http://git.example/pay-org/schemas.git"


def _body(repo):
    return ('{"ref":"refs/heads/main","repository":{"full_name":"%s"}}' % repo).encode()


@pytest.mark.asyncio
async def test_the_http_push_goes_to_the_tenant_of_the_pushed_repository(declared):
    app, mgr = _app(_by_repo)
    mgr.resolve_rule.return_value = {"platform": "github", "datasource": "mongo"}
    with patch("app.tasks.etl.process_webhook_task") as task:
        task.apply_async.return_value = MagicMock(id="t-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            headers = {"x-github-event": "push"}
            ok = await c.post("/v1/webhooks/mongo", content=_body("pay-org/api"), headers=headers)
            assert ok.status_code == 200
            assert task.apply_async.call_args.kwargs["kwargs"]["tenant_id"] == "payments"
            assert mgr.resolve_rule.call_args.args[2] == "payments"

            nobody = await c.post("/v1/webhooks/mongo", content=_body("evil-org/x"), headers=headers)
            assert nobody.status_code == 403 and "no declared tenant owns" in nobody.json()["detail"]

            # a header naming another tenant is checked against the repository, and loses
            lying = await c.post("/v1/webhooks/mongo", content=_body("pay-org/api"),
                                 headers={**headers, "x-tenant-id": "search"})
            assert lying.status_code == 403 and "another tenant" in lying.json()["detail"]
    assert task.apply_async.call_count == 1


@pytest.mark.asyncio
async def test_the_warm_up_reports_the_schema_commit_this_process_now_holds():
    app, mgr = _app(_by_repo)
    mgr.schema_commit.return_value = "f" * 40
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/v1/tenants/demo/schemas/warm")
    assert resp.status_code == 200
    assert resp.json() == {"tenant_id": "demo", "status": "ok", "rules_loaded": 0, "schema_commit": "f" * 40}
    mgr.invalidate.assert_called_once_with("demo")            # fetched afresh, not served from a held clone
    mgr.schema_commit.assert_called_once_with("demo")


@pytest.mark.asyncio
async def test_a_dry_run_previews_the_sync_of_the_tenant_the_push_belongs_to(declared):
    """The dry run asks the question the real push asks: the pushed repository decides the tenant and
    the rule. It used to take only the grant's tenant, so a push with a grant that names none was
    previewed for tenant "default": another rule, no schemas, a report of a sync that never runs."""
    app, mgr = _app(_by_repo)
    mgr.resolve_rule.return_value = {"platform": "github", "datasource": "mongo", "branch_as_env": True}
    with patch("app.tasks.etl.process_webhook_task") as task:
        task.apply_async.return_value = MagicMock(id="t-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            ok = await c.post("/v1/webhooks/mongo/dry-run?wait=false", content=_body("pay-org/api"),
                              headers={"x-github-event": "push"})
    assert ok.status_code == 200
    kwargs = task.apply_async.call_args.kwargs["kwargs"]
    assert kwargs["tenant_id"] == "payments" and kwargs["dry_run"] is True
    assert mgr.resolve_rule.call_args.args[2] == "payments"           # the rule of that tenant
    assert kwargs["rule_config"]["branch_as_env"] is True              # …which the real push would use too


@pytest.mark.asyncio
async def test_a_dry_run_is_refused_for_a_push_no_tenant_owns_or_a_grant_for_another_tenant(declared):
    app, mgr = _app(_by_repo)
    mgr.resolve_rule.return_value = {"platform": "github", "datasource": "mongo"}
    with patch("app.tasks.etl.process_webhook_task") as task:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            headers = {"x-github-event": "push"}
            nobody = await c.post("/v1/webhooks/mongo/dry-run", content=_body("evil-org/x"), headers=headers)
            lying = await c.post("/v1/webhooks/mongo/dry-run", content=_body("pay-org/api"),
                                 headers={**headers, "x-tenant-id": "search"})
    assert nobody.status_code == 403 and "no declared tenant owns" in nobody.json()["detail"]
    assert lying.status_code == 403 and "another tenant" in lying.json()["detail"]
    task.apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_a_dry_run_reads_a_gitea_push_as_gitea_like_the_real_push(declared):
    app, mgr = _app(_by_repo)
    mgr.resolve_rule.return_value = {"platform": "gitea", "datasource": "mongo"}
    with patch("app.tasks.etl.process_webhook_task") as task:
        task.apply_async.return_value = MagicMock(id="t-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/webhooks/mongo/dry-run?wait=false", content=_body("pay-org/api"),
                         headers={"x-gitea-event": "push"})
    assert task.apply_async.call_args.kwargs["kwargs"]["platform"] == "gitea"


@pytest.mark.asyncio
async def test_a_grants_tenant_must_be_the_tenant_of_the_push(declared, monkeypatch):
    app, mgr = _app(_by_repo)
    mgr.resolve_rule.return_value = {"platform": "github", "datasource": "mongo"}
    monkeypatch.setattr("app.api.deps.call_identity",
                        lambda request, datasource, platform: MagicMock(platform="github", tenant="search"))
    with patch("app.tasks.etl.process_webhook_task") as task:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/webhooks/mongo", content=_body("pay-org/api"), headers={"x-github-event": "push"})
    assert r.status_code == 403 and "another tenant" in r.json()["detail"]
    task.apply_async.assert_not_called()


def _grpc_webhook(rule, repo):
    from app.grpc_server.server import OrchestratorServicer

    class Context:
        code = None
        details = ""

        def set_code(self, code):
            self.code = code

        def set_details(self, details):
            self.details = details

    request = MagicMock(platform="github", datasource="mongo", raw_payload=_body(repo).decode(), headers={},
                        received_at=None)
    servicer = OrchestratorServicer({"github:mongo": rule})
    ctx = Context()
    with patch("app.tasks.etl.process_webhook_task") as task:
        task.apply_async.return_value = MagicMock(id="t-1")
        servicer.ProcessWebhook(request, ctx)
    return ctx, task


def test_the_grpc_push_goes_to_the_tenant_of_the_pushed_repository(declared):
    _, task = _grpc_webhook({"platform": "github", "datasource": "mongo", "tenant_id": "search"}, "pay-org/api")
    assert task.apply_async.call_args.kwargs["kwargs"]["tenant_id"] == "payments"   # not the rule's own


def test_the_grpc_push_from_a_repository_nobody_owns_is_refused(declared):
    import grpc
    ctx, task = _grpc_webhook({"platform": "github", "datasource": "mongo", "tenant_id": "search"}, "evil-org/x")
    assert ctx.code == grpc.StatusCode.PERMISSION_DENIED and "no declared tenant owns" in ctx.details
    task.apply_async.assert_not_called()


def test_a_rule_that_names_no_tenant_belongs_to_the_tenant_whose_repositories_it_covers(declared):
    from app.pipeline import rule_layers
    assert rule_layers.owner({"repositories": ["pay-org/api"]}) == "payments"
    assert rule_layers.owner({"repositories": ["pay-org/api"], "tenants": ["search"]}) == "search"
    assert rule_layers.owner({"repositories": ["unlisted/repo"]}) == "default"
    assert rule_layers.owner({"repositories": ["pay-org/api", "search-org/app"]}) == "default"


def test_with_no_declared_tenants_a_rule_without_one_is_the_default_tenants(monkeypatch):
    from app.pipeline import rule_layers
    monkeypatch.setattr(tenants, "declared", lambda: [])
    assert rule_layers.owner({"repositories": ["pay-org/api"]}) == "default"


def test_the_uis_tenants_are_the_declared_ones(declared):
    from app.api.routers import ui
    assert ui._all_tenants() == ["bare", "payments", "search"]


@pytest.mark.asyncio
async def test_the_ui_refuses_a_tenant_nobody_declared(declared):
    app, _ = _app(_by_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/v1/ui/bundle?tenant=EVIL")
    assert resp.status_code == 404 and "not declared" in resp.text


# ── Phase 5: presets come from the tenant's own sources, with the tenant's credential ──

def _scope(**kw):
    from app.pipeline.presets import Scope
    base = dict(tenant="payments", repositories=("pay-org/*",), schema_repo="pay-org/schemas",
                schema_token="schema-token", presets=("shared/presets",), presets_token="presets-token")
    base.update(kw)
    return Scope(**base)


def test_each_source_of_a_tenant_has_its_own_credential():
    scope = _scope()
    assert scope.token_for("pay-org/api", "platform-token") == "platform-token"
    assert scope.token_for("Pay-Org/Schemas", "platform-token") == "schema-token"     # its schema repository
    assert scope.token_for("shared/presets", "platform-token") == "presets-token"


def test_a_preset_anywhere_else_is_refused_and_the_platform_credential_never_goes_there():
    from app.pipeline.presets import PresetError
    with pytest.raises(PresetError, match="not one of tenant payments's preset sources"):
        _scope().token_for("evil/steal", "platform-token")


def test_a_resolver_refuses_a_foreign_preset_before_fetching_it():
    from app.pipeline.presets import Origin, PresetError, Resolver
    fetched = []

    def fetch(url, ref, path, token):
        fetched.append((url, token))
        return "{}"

    resolver = Resolver(Origin("http://gitea:3000", "platform-token"), fetch, _scope())
    resolver.resolve({"extends": ["local>pay-org/api:base", "local>pay-org/schemas:team", "local>shared/presets"]})
    assert fetched == [("http://gitea:3000/pay-org/api.git", "platform-token"),
                       ("http://gitea:3000/pay-org/schemas.git", "schema-token"),
                       ("http://gitea:3000/shared/presets.git", "presets-token")]

    fetched.clear()
    with pytest.raises(PresetError, match="preset local>evil/steal"):
        resolver.resolve({"extends": ["local>evil/steal"]})
    assert fetched == []


def test_with_no_scope_presets_are_as_open_as_they_were():
    from app.pipeline.presets import Origin, Resolver
    got = []
    Resolver(Origin("http://gitea:3000", "platform-token"), lambda *a: got.append(a) or "{}").resolve(
        {"extends": ["local>anyone/anything"]})
    assert got and got[0][3] == "platform-token"


def test_a_scope_is_only_made_for_a_declared_tenant(monkeypatch):
    from app.pipeline import presets
    monkeypatch.setattr(tenants, "declared", lambda: [dict(t) for t in DECLARED])
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: {})
    scope = presets.scope_of("payments")
    assert scope.repositories == ("pay-org/*",) and scope.schema_repo == "pay-org/schemas"
    assert scope.schema_token == "pay-token"
    assert presets.scope_of("default") is None and presets.scope_of(None) is None


def test_a_tenants_bundle_json_cannot_extend_a_preset_it_has_no_claim_on():
    from app.pipeline.presets import Origin, PresetError, resolve_bundle_rules
    data = {"rules": [{"platform": "gitea", "datasource": "mongo", "extends": ["local>evil/steal"]}]}
    with pytest.raises(PresetError, match="not one of tenant payments's preset sources"):
        resolve_bundle_rules(data, Origin("http://gitea:3000"), "bundle.json", lambda *a: "{}", _scope())


def test_a_vartrack_json_that_extends_a_foreign_preset_stops_the_sync(monkeypatch):
    from app.pipeline import repo_overrides
    monkeypatch.setattr("app.pipeline.presets.scope_of", lambda tenant: _scope() if tenant == "payments" else None)
    monkeypatch.setattr("app.pipeline.git_extractor.GitExtractor.extract_file",
                        lambda self, *a, **kw: '{"extends": ["local>evil/steal"]}')
    from app.pipeline.models import PayloadContext
    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/pay-org/api.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"token": "platform-token", "tenant_id": "other"}, parsed_payload={})
    with pytest.raises(repo_overrides.RepoConfigError, match="not one of tenant payments's preset sources"):
        repo_overrides.apply_repo_overrides(ctx, "payments")
    # a tenant with no declared scope is untouched
    monkeypatch.setattr("app.pipeline.presets._git_fetch", lambda *a: "{}")
    repo_overrides.apply_repo_overrides(ctx, "undeclared")


def test_validate_config_reports_a_preset_the_tenant_could_not_use(monkeypatch):
    from app import config_validator
    monkeypatch.setattr("app.pipeline.presets.scope_of", lambda tenant: _scope() if tenant == "payments" else None)
    body = '{"extends": ["local>evil/steal"]}'
    bad = config_validator.validate("vartrack.json", body, "payments")
    assert any("not one of tenant payments's preset sources" in e for e in bad.errors)
    assert not config_validator.validate("vartrack.json", body, "other").errors


@pytest.mark.asyncio
async def test_a_tenant_route_refuses_a_tenant_nobody_declared(declared):
    app, _ = _app(_by_repo, listed=("payments",))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/v1/tenants/EVIL/schemas")
    assert resp.status_code == 404 and "not declared" in resp.text


def test_a_declared_schema_repository_named_owner_slash_name_is_cloned_from_its_platforms_server(monkeypatch):
    mgr = _manager(monkeypatch)
    monkeypatch.setattr("app.pipeline.presets._platform", lambda name: ("http://gitea:3000", "platform-token"))
    declared_tenants = [{"name": "demo", "repositories": ["vartrack-demo/*"],
                         "schema_repo": {"platform": "github", "repo": "vartrack-demo/schemas", "branch": "main"}}]
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: {"tenants": declared_tenants})
    reg = mgr.registration("demo")
    assert reg["repo_url"] == "http://gitea:3000/vartrack-demo/schemas.git"
    assert reg["token"] == "platform-token" and mgr.tenant_for_repo("vartrack-demo/schemas") == "demo"


def test_a_schema_repository_on_a_platform_config_cue_lacks_is_the_tenants_error(monkeypatch):
    from app.dependencies import SchemaUnavailable
    from app.pipeline.presets import PresetError

    mgr = _manager(monkeypatch)

    def missing(name):
        raise PresetError(f"no platform {name!r} in config.cue")

    monkeypatch.setattr("app.pipeline.presets._platform", missing)
    declared_tenants = [{"name": "demo", "schema_repo": {"platform": "gitea", "repo": "vartrack-demo/schemas"}}]
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: {"tenants": declared_tenants})
    with pytest.raises(SchemaUnavailable, match="names the platform 'gitea'"):
        mgr.get_or_clone("demo")
