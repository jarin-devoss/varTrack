"""
The rule a sync runs with is built in layers (app.pipeline.rule_layers):
config.cue's rule, the tenant's bundle.json rule, the repository's
vartrack.json, then config.cue's `force`. Keys only config.cue sets are dropped from the layers below it.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline import datasource_settings as ds
from app.pipeline import rule_layers
from app.pipeline.models import PayloadContext
from app.pipeline.rule_layers import apply_force, layered_rule, merge_child, rule_for, tenant_rules

CUE = {
    "platforms": [
        {"gitea": {"endpoint": "http://gitea:3000/"}},
        {"github": {"tag": "cloud"}},
    ],
    "datasources": [{"mongo": {"tag": "primary", "endpoint": "mongodb://mongo:27017"}}],
    "rules": [
        {"platform": "gitea", "datasource": "mongo-primary", "repositories": ["acme/configs"],
         "file_name": "app.yaml", "branch_map": {"main": "production"}, "sync_mode": "SYNC_MODE_AUTO",
         "prune": {"enabled": True}, "self_heal": True, "secret_manager": "vault-prod"},
        {"platform": "gitea", "datasource": "redis", "repositories": ["acme/*"], "file_name": "app.yaml",
         "force": {"prune": {"enabled": False}, "dry_run": True, "self_heal": False}},
        {"platform": "github-cloud", "datasource": "zookeeper", "repositories": ["acme/zk"],
         "file_name": "zk.yaml"},
    ],
}


@pytest.fixture(autouse=True)
def cue(monkeypatch):
    rule_layers._warned.clear()
    monkeypatch.setattr(ds, "_bundle", lambda: CUE)


def test_bundle_json_overrides_config_cue_and_maps_merge():
    rule = layered_rule(CUE["rules"][0], {
        "platform": "gitea", "datasource": "mongo-primary", "tenant_id": "acme",
        "branch_map": {"develop": "staging"}, "sync_mode": "GIT_SMART_REPAIR", "prune": {"dry_run": True},
        "root_key": "app",
    }, "acme")
    assert rule["branch_map"] == {"main": "production", "develop": "staging"}
    assert rule["sync_mode"] == "GIT_SMART_REPAIR"
    assert rule["prune"] == {"enabled": True, "dry_run": True}
    assert rule["root_key"] == "app"
    assert rule["file_name"] == "app.yaml"
    assert rule["tenant_id"] == "acme"


def test_keys_only_config_cue_sets_are_dropped_from_bundle_json(caplog):
    caplog.set_level(logging.WARNING)
    rule = layered_rule(CUE["rules"][0], {
        "platform": "gitea", "datasource": "mongo-primary",
        "mongo_uri": "mongodb://elsewhere", "database": "other",
        "secret_manager": "vault-dev", "repositories": ["acme/other"], "self_heal": False,
    }, "acme")
    for key in ("mongo_uri", "database"):
        assert key not in rule
    assert rule["secret_manager"] == "vault-prod"
    assert rule["repositories"] == ["acme/configs"]
    assert rule["self_heal"] is True
    assert "database, mongo_uri, repositories, secret_manager, self_heal" in caplog.text


def test_a_rule_level_secrets_block_is_dropped_and_says_what_to_do_instead(caplog):
    # It was removed: the secret managers are config.cue's secret_managers.
    caplog.set_level(logging.WARNING)
    rule = layered_rule(CUE["rules"][0], {
        "platform": "gitea", "datasource": "mongo-primary", "secrets": {"default": {"type": "vault"}},
    }, "acme")
    assert "secrets" not in rule
    assert "secrets was removed and has no effect" in caplog.text and "secret_managers" in caplog.text


def test_force_wins_but_cannot_pin_keys_only_config_cue_sets(caplog):
    caplog.set_level(logging.WARNING)
    rule = layered_rule(CUE["rules"][1], {"platform": "gitea", "datasource": "redis",
                                          "prune": {"enabled": True, "dry_run": True}, "dry_run": False}, "default")
    assert rule["prune"] == {"enabled": False}
    assert rule["dry_run"] is True
    assert "self_heal" not in rule
    assert "forces self_heal" in caplog.text


def test_repo_url_from_the_platform_for_a_single_repository():
    assert layered_rule(CUE["rules"][0], None, "default")["repo_url"] == "http://gitea:3000/acme/configs.git"
    assert layered_rule(CUE["rules"][2], None, "default")["repo_url"] == "https://github.com/acme/zk.git"
    assert "repo_url" not in layered_rule(CUE["rules"][1], None, "default")
    assert layered_rule(CUE["rules"][0], {"platform": "gitea", "datasource": "mongo-primary",
                                          "repo_url": "http://mirror/acme/configs.git"}, "t")["repo_url"] \
        == "http://mirror/acme/configs.git"


def test_tenant_rules():
    bundle_rules = [
        {"platform": "gitea", "datasource": "mongo-primary", "root_key": "app"},
        {"platform": "gitea", "datasource": "nowhere"},
    ]
    claimed = {("acme", "gitea", "mongo-primary"), ("acme", "gitea", "nowhere")}
    acme = tenant_rules(bundle_rules, "acme", claimed)
    assert [(r["datasource"], r["tenant_id"]) for r in acme] == [("mongo-primary", "acme")]
    assert acme[0]["root_key"] == "app"
    # "default" holds the config.cue rules no bundle.json names.
    default = tenant_rules([], "default", claimed)
    assert [r["datasource"] for r in default] == ["redis", "zookeeper"]


def test_rule_for():
    claimed = {("acme", "gitea", "mongo-primary")}
    assert rule_for("gitea", "mongo-primary", None, "acme", claimed) is None
    assert rule_for("gitea", "mongo-primary", None, "default", claimed) is None
    assert rule_for("gitea", "redis", None, "default", claimed)["datasource"] == "redis"
    assert rule_for("gitea", "redis", None, "acme", claimed) is None
    assert rule_for("gitea", "nowhere", {"platform": "gitea", "datasource": "nowhere"}, "acme", claimed) is None


def test_without_rules_in_config_cue_bundle_json_is_used_as_is(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: {"datasources": CUE["datasources"]})
    rule = {"platform": "gitea", "datasource": "mongo-primary", "mongo_uri": "mongodb://x"}
    assert rule_for("gitea", "mongo-primary", rule, "acme", set()) is rule
    assert tenant_rules([rule], "acme", set()) == [rule]


def test_a_rule_with_tenants_belongs_to_them(monkeypatch):
    # Only the rule's tenants layer it, and the first one owns it when no
    # bundle.json names it.
    cue = {**CUE, "rules": [{**CUE["rules"][0], "tenants": ["payments", "ops"]}, *CUE["rules"][1:]]}
    monkeypatch.setattr(ds, "_bundle", lambda: cue)
    mine = {"platform": "gitea", "datasource": "mongo-primary", "root_key": "x"}

    assert tenant_rules([mine], "acme", {("acme", "gitea", "mongo-primary")}) == []
    assert rule_for("gitea", "mongo-primary", mine, "acme", set()) is None
    assert rule_for("gitea", "mongo-primary", mine, "ops", set())["root_key"] == "x"

    # acme's claim does not count: the rule is still payments'.
    claimed = {("acme", "gitea", "mongo-primary")}
    assert [r["datasource"] for r in tenant_rules([], "payments", claimed)] == ["mongo-primary"]
    assert [r["datasource"] for r in tenant_rules([], "default", claimed)] == ["redis", "zookeeper"]
    assert rule_for("gitea", "mongo-primary", None, "payments", claimed)["tenant_id"] == "payments"
    assert rule_for("gitea", "mongo-primary", None, "default", claimed) is None
    # ops names it: it is ops'.
    assert rule_for("gitea", "mongo-primary", None, "payments", {("ops", "gitea", "mongo-primary")}) is None
    assert rule_layers.owners() == ["payments", "default"]


def test_bundle_json_cannot_set_tenants():
    rule = layered_rule(CUE["rules"][0], {"platform": "gitea", "datasource": "mongo-primary",
                                          "tenants": ["acme"]}, "acme")
    assert "tenants" not in rule


def test_merge_child_replaces_other_keys():
    assert merge_child({"prune": True, "file_name": "a"}, {"prune": {"enabled": False}}) == \
        {"prune": {"enabled": False}, "file_name": "a"}
    assert apply_force({"dry_run": False}) == {"dry_run": False}


def test_vartrack_json_merges_and_force_is_applied_after_it():
    from app.pipeline.repo_overrides import apply_repo_overrides

    rule = layered_rule(CUE["rules"][1], None, "default")
    rule["branch_map"] = {"main": "production"}
    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/acme/a.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config=rule, parsed_payload={})
    vartrack_json = json.dumps({"branch_map": {"develop": "staging"}, "dry_run": False,
                                "prune": {"enabled": True}})
    with patch("app.pipeline.git_extractor.GitExtractor") as extractor:
        extractor.return_value = MagicMock(extract_file=MagicMock(return_value=vartrack_json))
        out = apply_repo_overrides(ctx).rule_config
    assert out["branch_map"] == {"main": "production", "develop": "staging"}
    assert out["dry_run"] is True
    assert out["prune"] == {"enabled": False}


def test_schema_manager_layers_its_rules(monkeypatch, tmp_path):
    from app.config import settings
    from app.schema_registry import manager as manager_mod

    monkeypatch.setattr(settings, "SCHEMA_CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.utils.redis_cache.get_cache", lambda: None)
    mgr = manager_mod.SchemaRegistryManager()
    mgr._entries["acme:repo"] = manager_mod._TenantEntry(
        tenant_id="acme", repo_url="", branch="main", local_path=tmp_path,
        rules=[{"platform": "gitea", "datasource": "mongo-primary", "mongo_uri": "mongodb://x", "root_key": "app"}],
    )
    rule = mgr.resolve_rule("gitea", "mongo-primary", "acme")
    assert rule["root_key"] == "app" and "mongo_uri" not in rule and rule["file_name"] == "app.yaml"
    assert mgr.resolve_rule("gitea", "mongo-primary", "default") is None
    assert mgr.resolve_rule("gitea", "redis", "default")["datasource"] == "redis"
    assert [r["datasource"] for r in mgr.get_all_rules("default")] == ["redis", "zookeeper"]


@pytest.mark.parametrize("rule,want", [
    ({"prune": {"enabled": True, "protection": ["SYSTEM_*"]}}, (True, False, ["SYSTEM_*"])),
    ({"prune": {"enabled": False}}, (False, False, [])),
    ({"prune": True, "prune_protection": ["_vt_*"]}, (True, False, ["_vt_*"])),
    ({"prune": {"enabled": True}, "prune_protection": ["_vt_*"]}, (True, False, ["_vt_*"])),
    ({"prune": {"protection": ["_vt_*"]}}, (False, False, ["_vt_*"])),
    ({"prune": {"dry_run": False}}, (True, False, [])),
    # `last` was removed: it is not read, and a block with only it is disabled.
    ({"prune": {"last": True}}, (False, False, [])),
    ({"prune": {"last": True, "dry_run": True}}, (True, True, [])),
    ({"prune": True, "prune_last": True}, (True, False, [])),
])
def test_prune_from_config_cue_and_from_the_old_flat_keys(rule, want):
    from app.tasks.payload import extract_prune_config
    from app.ui.views import _prune

    p = extract_prune_config(rule)
    assert (p.enabled, p.dry_run, p.protection) == want
    assert _prune(rule) == {"enabled": want[0], "dryRun": want[1], "protection": want[2]}


def test_a_broken_bundle_json_leaves_its_tenant_without_rules(monkeypatch, tmp_path):
    # Tenant "default" must not fall back to config.cue's rules without its
    # layer when its bundle.json cannot be parsed.
    from app.config import settings
    from app.schema_registry import manager as manager_mod

    monkeypatch.setattr(settings, "SCHEMA_CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.utils.redis_cache.get_cache", lambda: None)
    (tmp_path / "bundle.json").write_text('{"rules": [', encoding="utf-8")
    rules, error = manager_mod._load_bundle_checked(tmp_path)
    assert rules == [] and error.startswith("bundle.json:")

    mgr = manager_mod.SchemaRegistryManager()
    mgr._entries["default:repo"] = manager_mod._TenantEntry(
        tenant_id="default", repo_url="", branch="main", local_path=tmp_path, bundle_error=error)
    assert mgr.get_all_rules("default") == []
    assert mgr.resolve_rule("gitea", "redis", "default") is None


def test_concurrent_schema_clones_do_not_share_a_directory(monkeypatch, tmp_path):
    # Worker processes share the schema cache volume: two clones of one
    # tenant at once must not write into the same directory.
    from unittest.mock import MagicMock

    import git

    from app.config import settings
    from app.schema_registry import manager as manager_mod

    def fake_clone(url, path, **kw):
        (manager_mod.Path(path) / "bundle.json").write_text("[]", encoding="utf-8")
        return MagicMock(head=MagicMock(commit=MagicMock(hexsha="abc")))

    monkeypatch.setattr(settings, "SCHEMA_CACHE_DIR", tmp_path)
    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(fake_clone))
    mgr = manager_mod.SchemaRegistryManager()
    mgr._root = tmp_path
    a = mgr._clone("acme", "http://gitea:3000/acme/schemas.git", "main", None)
    b = mgr._clone("acme", "http://gitea:3000/acme/schemas.git", "main", None)
    assert a.local_path != b.local_path
    assert a.local_path.parent == b.local_path.parent == tmp_path / "acme"
    assert a.commit_sha == b.commit_sha == "abc"
