"""
The watcher does not read config.cue, bundle.json or vartrack.json: it asks the
orchestrator which rules to keep in sync (ListWatchTargets), on every poll, and
follows rule changes without a restart. The orchestrator answers from the same
rules a drift check works on (app.ui.drift.tenant_rules).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.grpc_server.server import OrchestratorServicer
from app.ui import drift


class FakeManager:
    def __init__(self, rules_by_tenant, *, broken_clone=()):
        self.rules_by_tenant = rules_by_tenant
        self.broken_clone = set(broken_clone)
        self.cloned = []

    def list_tenant_ids(self):
        return list(self.rules_by_tenant)

    def get_or_clone(self, tenant):
        self.cloned.append(tenant)
        if tenant in self.broken_clone:
            raise OSError("git unreachable")

    def get_all_rules(self, tenant):
        return self.rules_by_tenant.get(tenant, [])


@pytest.fixture
def manager(monkeypatch):
    def install(rules_by_tenant, **kw):
        mgr = FakeManager(rules_by_tenant, **kw)
        monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: mgr)
        return mgr
    return install


def _rule(platform, datasource, **kw):
    return {"platform": platform, "datasource": datasource, **kw}


def test_only_rules_with_self_heal_that_are_not_suspended_are_targets(manager):
    manager({"default": [
        _rule("github", "mongo", self_heal=True),
        _rule("github", "redis", self_heal=False),
        _rule("github", "s3"),
        _rule("github", "zookeeper", self_heal=True, suspend=True),
    ]})

    assert [(t, r["datasource"]) for t, r in drift.watch_targets()] == [("default", "mongo")]


def test_every_tenants_rules_are_listed_with_the_tenant(manager):
    manager({
        "acme": [_rule("gitea", "mongo", self_heal=True)],
        "globex": [_rule("gitea", "mongo", self_heal=True), _rule("gitea", "redis", self_heal=True)],
    })

    got = [(t, r["platform"], r["datasource"]) for t, r in drift.watch_targets()]

    assert got == [("acme", "gitea", "mongo"), ("globex", "gitea", "mongo"), ("globex", "gitea", "redis")]


def test_with_no_tenant_registered_the_default_tenant_is_asked(manager):
    mgr = manager({})
    mgr.list_tenant_ids = lambda: []
    mgr.rules_by_tenant = {"default": [_rule("github", "mongo", self_heal=True)]}

    assert [t for t, _ in drift.watch_targets()] == ["default"]


def test_a_tenant_whose_schema_repo_cannot_be_refreshed_still_answers_with_its_loaded_rules(manager):
    mgr = manager({"acme": [_rule("gitea", "mongo", self_heal=True)]}, broken_clone={"acme"})

    assert [t for t, _ in drift.watch_targets()] == ["acme"]
    assert mgr.cloned == ["acme"]


def test_the_rpc_answers_with_platform_datasource_and_tenant(manager):
    manager({"acme": [_rule("gitea-team", "mongo-primary", self_heal=True)]})

    resp = OrchestratorServicer({}).ListWatchTargets(MagicMock(), MagicMock())

    assert [(t.platform, t.datasource, t.tenant_id) for t in resp.targets] == [("gitea-team", "mongo-primary", "acme")]


def test_the_rpc_answers_an_empty_list_when_nothing_self_heals(manager):
    manager({"default": [_rule("github", "mongo")]})

    assert list(OrchestratorServicer({}).ListWatchTargets(MagicMock(), MagicMock()).targets) == []
