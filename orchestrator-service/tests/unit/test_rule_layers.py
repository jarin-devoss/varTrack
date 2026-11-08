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
         "force": {"prune": {"enabled": False}, "strict_validation": True, "self_heal": False}},
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
        "branch_map": {"develop": "staging"}, "sync_mode": "GIT_SMART_REPAIR", "prune": {"protection": ["keep.*"]},
        "root_key": "app",
    }, "acme")
    assert rule["branch_map"] == {"main": "production", "develop": "staging"}
    assert rule["sync_mode"] == "GIT_SMART_REPAIR"
    assert rule["prune"] == {"enabled": True, "protection": ["keep.*"]}
    assert rule["root_key"] == "app"
    assert rule["file_name"] == "app.yaml"
    assert rule["tenant_id"] == "acme"


def test_keys_only_config_cue_sets_are_dropped_from_bundle_json(caplog):
    caplog.set_level(logging.WARNING)
    rule = layered_rule(CUE["rules"][0], {
        "platform": "gitea", "datasource": "mongo-primary",
        "endpoint": "mongodb://elsewhere", "database": "other",
        "secret_manager": "vault-dev", "repositories": ["acme/other"], "self_heal": False,
    }, "acme")
    for key in ("endpoint", "database"):   # the datasource's fields: config.cue's block alone sets them
        assert key not in rule
    assert rule["secret_manager"] == "vault-prod"
    assert rule["repositories"] == ["acme/configs"]
    assert rule["self_heal"] is True
    assert "database, endpoint, repositories, secret_manager, self_heal" in caplog.text


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
                                          "prune": {"enabled": True}, "strict_validation": False}, "default")
    assert rule["prune"] == {"enabled": False}
    assert rule["strict_validation"] is True
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
    # Used as it is, except for the tenant: that is the one whose schema repository it was loaded
    # from, whatever the file says.
    assert rule_for("gitea", "mongo-primary", rule, "acme", set()) == {**rule, "tenant_id": "acme"}
    assert tenant_rules([rule], "acme", set()) == [{**rule, "tenant_id": "acme"}]
    claiming = {**rule, "tenant_id": "someone-else"}
    assert rule_for("gitea", "mongo-primary", claiming, "acme", set())["tenant_id"] == "acme"
    assert tenant_rules([claiming], "acme", set())[0]["tenant_id"] == "acme"


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
    assert apply_force({"strict_validation": False}) == {"strict_validation": False}


def test_vartrack_json_merges_and_force_is_applied_after_it():
    from app.pipeline.repo_overrides import apply_repo_overrides

    rule = layered_rule(CUE["rules"][1], None, "default")
    rule["branch_map"] = {"main": "production"}
    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/acme/a.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config=rule, parsed_payload={})
    vartrack_json = json.dumps({"branch_map": {"develop": "staging"}, "strict_validation": False,
                                "prune": {"enabled": True}})
    with patch("app.pipeline.git_extractor.GitExtractor") as extractor:
        extractor.return_value = MagicMock(extract_file=MagicMock(return_value=vartrack_json))
        out = apply_repo_overrides(ctx).rule_config
    assert out["branch_map"] == {"main": "production", "develop": "staging"}
    assert out["strict_validation"] is True
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
    ({"prune": {"enabled": True, "protection": ["SYSTEM_*"]}}, (True, ["SYSTEM_*"])),
    ({"prune": {"enabled": False}}, (False, [])),
    ({"prune": True, "prune_protection": ["_vt_*"]}, (True, ["_vt_*"])),
    ({"prune": {"enabled": True}, "prune_protection": ["_vt_*"]}, (True, ["_vt_*"])),
    ({"prune": {"protection": ["_vt_*"]}}, (False, ["_vt_*"])),
    # `last` was removed: it is not read, and a block with only it is disabled.
    ({"prune": {"last": True}}, (False, [])),
    ({"prune": {"enabled": True, "last": True}}, (True, [])),
    ({"prune": True, "prune_last": True}, (True, [])),
    # `dry_run` was removed the same way: a block with only it is a disabled block.
    ({"prune": {"dry_run": True}}, (False, [])),
])
def test_prune_from_config_cue_and_from_the_old_flat_keys(rule, want):
    from app.tasks.payload import extract_prune_config
    from app.ui.views import _prune

    p = extract_prune_config(rule)
    assert (p.enabled, p.protection) == want
    assert _prune(rule) == {"enabled": want[0], "protection": want[1]}


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
    # not "no such rule": the tenant's layer is unusable, and the sync says so
    from app.dependencies import SchemaUnavailable
    with pytest.raises(SchemaUnavailable, match="bundle.json"):
        mgr.resolve_rule("gitea", "redis", "default")


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


# ── schema_map ───────────────────────────────────────────────────────────────

OPERATOR = {"match": "services/api/**", "schema": "api.cue"}
TENANT = {"match": "services/**", "schema": "tenant.cue"}


def test_schema_map_adds_up_across_layers_with_the_operators_entries_first():
    cue_rule = {**CUE["rules"][0], "schema_map": [OPERATOR]}
    bundle_rule = {"platform": "gitea", "datasource": "mongo-primary", "schema_map": [TENANT]}
    rule = layered_rule(cue_rule, bundle_rule, "acme")
    assert rule["schema_map"] == [OPERATOR, TENANT]       # the first match wins: a pinned path cannot be remapped


def test_a_tenants_schema_map_alone_or_the_operators_alone_is_kept():
    only_tenant = layered_rule(CUE["rules"][0], {"platform": "gitea", "datasource": "mongo-primary",
                                                 "schema_map": [TENANT]}, "acme")
    only_operator = layered_rule({**CUE["rules"][0], "schema_map": [OPERATOR]}, None, "acme")
    assert only_tenant["schema_map"] == [TENANT] and only_operator["schema_map"] == [OPERATOR]
    assert "schema_map" not in layered_rule(CUE["rules"][0], None, "acme")


def test_a_repository_cannot_set_schema_map_or_schema_from_file(caplog):
    from app.pipeline import rule_options
    from app.pipeline.repo_overrides import _OVERRIDABLE_KEYS

    assert "schema_map" in rule_options.TENANT_SETTABLE and "schema_from_file" in rule_options.TENANT_SETTABLE
    assert "schema_map" not in _OVERRIDABLE_KEYS and "schema_from_file" not in _OVERRIDABLE_KEYS


def test_a_bundle_json_with_a_bad_schema_map_entry_is_unusable_and_says_why(tmp_path):
    from app.schema_registry import manager as manager_mod

    (tmp_path / "bundle.json").write_text(json.dumps({"rules": [
        {"platform": "gitea", "datasource": "mongo-primary",
         "schema_map": [{"match": "a/**", "schema": "ok.cue"}, {"match": "", "schema": "../x.cue"}]},
    ]}), encoding="utf-8")
    rules, error = manager_mod._load_bundle_checked(tmp_path)
    assert rules == []
    assert error.startswith("bundle.json: rules[0].schema_map[1].match") and 'schema_map[1].schema "../x.cue"' in error


def test_a_good_schema_map_in_bundle_json_loads(tmp_path):
    from app.schema_registry import manager as manager_mod

    entry = {"match": "a/**", "schema": "none"}
    (tmp_path / "bundle.json").write_text(json.dumps({"rules": [
        {"platform": "gitea", "datasource": "mongo-primary", "schema_map": [entry]}]}), encoding="utf-8")
    rules, error = manager_mod._load_bundle_checked(tmp_path)
    assert error == "" and rules[0]["schema_map"] == [entry]


def test_a_schema_map_target_missing_from_the_schema_repository_is_logged_once(tmp_path, caplog):
    from app.schema_registry import manager as manager_mod

    manager_mod._missing_logged.clear()
    (tmp_path / "there.cue").write_text("{}\n")
    rules = [{"platform": "gitea", "datasource": "mongo-primary", "schema_map": [
        {"match": "a/**", "schema": "there.cue"}, {"match": "b/**", "schema": "gone.cue"},
        {"match": "c/**", "schema": "none"}, {"match": "d/**", "schema": "../escape.cue"}]}]
    with caplog.at_level(logging.WARNING, logger="app.schema_registry.manager"):
        manager_mod._log_missing_schemas("acme", tmp_path, rules, "abc123" * 6)
        manager_mod._log_missing_schemas("acme", tmp_path, rules, "abc123" * 6)     # a refresh of the same commit
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if "gone.cue" in m]) == 1
    assert "bundle.json rule gitea/mongo-primary schema_map[1]" in messages[0] and "b/**" in messages[0]
    assert not any("there.cue" in m or "none" in m.split("schema")[-1] for m in messages if "gone.cue" not in m)


# ── the tenant is never something a file says ─────────────────────────────────

def _claiming(tenant):
    return {"platform": "gitea", "datasource": "mongo-primary", "tenant_id": tenant, "branch_map": {"develop": "staging"}}


def test_a_bundle_json_that_writes_a_tenant_id_is_told_where_the_tenant_comes_from(caplog):
    with caplog.at_level(logging.WARNING, logger="app.pipeline.rule_layers"):
        rule = layered_rule(CUE["rules"][0], _claiming("acme"), "acme")
    [message] = [r.getMessage() for r in caplog.records if "tenant_id" in r.getMessage()]
    assert message == ("bundle.json: the gitea/mongo-primary rule sets tenant_id, which no file may set — the tenant is "
                       "the one whose schema repository holds this file (acme); ignored")
    assert rule["branch_map"] == {"main": "production", "develop": "staging"}      # the rest of the rule is used


def test_a_tenant_id_that_names_another_tenant_is_named_and_cannot_act_as_it(caplog):
    with caplog.at_level(logging.WARNING, logger="app.pipeline.rule_layers"):
        rule = layered_rule(CUE["rules"][0], _claiming("evil"), "acme")
    assert any("it names tenant evil, but this file is tenant acme's" in r.getMessage() for r in caplog.records)
    assert not any("not an option a tenant may set" in r.getMessage() and "tenant_id" in r.getMessage()
                   for r in caplog.records)          # one specific warning, not also the generic one
    assert rule["tenant_id"] == "acme"


def test_the_warning_is_given_once_per_file(caplog):
    with caplog.at_level(logging.WARNING, logger="app.pipeline.rule_layers"):
        for _ in range(3):
            layered_rule(CUE["rules"][0], _claiming("evil"), "acme")
    assert len([r for r in caplog.records if "tenant_id" in r.getMessage()]) == 1


def test_without_rules_in_config_cue_the_claim_is_dropped_with_the_same_warning(monkeypatch, caplog):
    monkeypatch.setattr(ds, "_bundle", lambda: {})
    with caplog.at_level(logging.WARNING, logger="app.pipeline.rule_layers"):
        [rule] = tenant_rules([_claiming("evil")], "acme", set())
        again = rule_for("gitea", "mongo-primary", _claiming("evil"), "acme", set())
    assert rule["tenant_id"] == "acme" and again["tenant_id"] == "acme"
    assert any("which no file may set" in r.getMessage() for r in caplog.records)


def test_the_stamp_is_still_what_its_readers_get_when_a_file_names_another_tenant(monkeypatch):
    """The tenant varTrack records for a rule is read by self-heal (tasks/etl.sync_all), by the push's owner when no
    tenant is declared (tenants.tenant_of_push) and by the preset scope of a vartrack.json. A file's tenant_id must
    never reach them."""
    from app import tenants

    rule = layered_rule(CUE["rules"][0], _claiming("evil"), "acme")
    assert rule["tenant_id"] == "acme"                              # what sync_all dispatches the heal as
    monkeypatch.setattr(tenants, "declared_names", lambda: [])
    assert tenants.tenant_of_push(rule, ["acme/configs"]) == "acme"


def test_a_vartrack_json_that_writes_a_tenant_id_is_dropped_with_a_warning(caplog):
    from unittest.mock import MagicMock, patch

    from app.pipeline.repo_overrides import apply_repo_overrides

    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/acme/app.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"tenant_id": "acme", "prune": {"enabled": True}}, parsed_payload={})
    content = json.dumps({"tenant_id": "evil", "branch_map": {"main": "production"}})
    with patch("app.pipeline.git_extractor.GitExtractor") as extractor, \
            caplog.at_level(logging.WARNING, logger="app.pipeline.rule_layers"):
        extractor.return_value = MagicMock(extract_file=MagicMock(return_value=content))
        out = apply_repo_overrides(ctx).rule_config
    assert out["tenant_id"] == "acme" and out["branch_map"] == {"main": "production"}
    assert any("the tenant is the one that owns the pushed repository; ignored" in r.getMessage()
               for r in caplog.records)
