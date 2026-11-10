"""
The config validator's prune warnings: prune.last was removed, and prune on a
sink that replaces a whole object on every write has nothing to delete.
"""
from __future__ import annotations

import json

import pytest

from app import config_validator


@pytest.fixture(autouse=True)
def no_config_cue(monkeypatch):
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: {})


def _warnings(content: dict) -> list[str]:
    return config_validator.validate("vartrack.json", json.dumps(content)).warnings


def test_a_prune_block_with_only_last_is_reported_as_disabled():
    (warning,) = _warnings({"prune": {"last": True}})
    assert "prune.last was removed" in warning
    assert "disabled" in warning and "prune: true" in warning


def test_last_next_to_an_enabled_prune_is_only_reported_as_removed():
    for options in ({"prune": {"enabled": True, "last": True}}, {"prune": True, "prune_last": True},
                    {"prune": {"enabled": True, "last": False}}):
        (warning,) = [w for w in _warnings(options) if "prune.last was removed" in w]
        assert "is disabled" not in warning


def test_a_prune_without_last_is_not_reported():
    assert _warnings({"prune": {"enabled": True, "protection": ["x"]}}) == []


# (datasource, its block in config.cue, whether prune can delete anything)
@pytest.mark.parametrize("datasource,block,effective", [
    ("s3", {"bucket": "b"}, False),
    ("configmap-app", {}, False),
    ("helm", {}, False),
    ("linux_server-web", {}, False),
    ("redis", {"data_structure": "JSON"}, False),
    ("mongo-primary", {"update_strategy": "STRATEGY_FILE"}, False),
    ("mongo-primary", {"update_strategy": "STRATEGY_DOCUMENT"}, False),
    ("redis", {}, True),
    ("mongo", {"update_strategy": "STRATEGY_KEY_VALUE"}, True),
    ("zookeeper", {}, True),
])
def test_prune_is_reported_only_where_the_sink_replaces_a_whole_object(monkeypatch, datasource, block, effective):
    kind, _, tag = datasource.partition("-")
    bundle = {"datasources": [{kind: {**({"tag": tag} if tag else {}), **block}}]}
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: bundle)
    result = config_validator.Result()
    config_validator._prune_without_effect({"prune": True}, datasource, "bundle.json: the rule", result)
    assert (result.warnings == []) is effective
    if not effective:
        assert "prune has no effect" in result.warnings[0] and "on every write" in result.warnings[0]


def test_nothing_is_reported_when_prune_is_off():
    result = config_validator.Result()
    config_validator._prune_without_effect({"prune": False}, "s3", "bundle.json: the rule", result)
    assert result.warnings == []


# ── A rule is identified by its platform and datasource ───────────────────────

def _rules(*pairs: tuple[str, str]) -> list[dict]:
    return [{"platform": p, "datasource": d} for p, d in pairs]


def test_a_repeated_platform_and_datasource_is_an_error_that_names_a_way_out():
    result = config_validator.Result()
    config_validator._repeated_rules(
        _rules(("github", "mongo"), ("github", "redis"), ("github", "mongo")), "bundle.json", result)
    (error,) = result.errors
    assert "rule 3 repeats github/mongo (rule 1)" in error
    assert "tagged datasource (for example mongo-db)" in error


def test_the_same_datasource_on_two_platforms_or_two_datasources_on_one_is_fine():
    result = config_validator.Result()
    config_validator._repeated_rules(_rules(("github", "mongo-primary"), ("gitea-internal", "mongo-primary")), "x", result)
    config_validator._repeated_rules(_rules(("github", "mongo-app"), ("github", "mongo-db")), "x", result)
    config_validator._repeated_rules([{"datasource": "mongo"}, {"datasource": "mongo"}], "x", result)  # no platform: reported elsewhere
    assert result.errors == []


def test_a_bundle_json_with_a_repeated_rule_is_refused(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: MagicMock(registration=lambda t: {}))
    content = json.dumps({"rules": _rules(("github", "mongo"), ("github", "mongo"))})

    result = config_validator.validate("bundle.json", content)

    assert any("rule 2 repeats github/mongo (rule 1)" in e for e in result.errors)


def test_a_repeated_rule_in_config_cue_is_reported_too(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: MagicMock(registration=lambda t: {}))
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle",
                        lambda: {"rules": _rules(("github", "mongo"), ("github", "mongo"))})

    result = config_validator.validate("bundle.json", json.dumps({"rules": []}))

    assert any(e.startswith("config.cue: rule 2 repeats github/mongo") for e in result.errors)


def test_the_orchestrator_says_so_once_when_it_takes_the_first_of_two_rules(caplog):
    import logging

    from app.pipeline import rule_layers

    rule_layers._warned.clear()
    bundle = {"rules": _rules(("github", "mongo"), ("github", "mongo"))}
    with caplog.at_level(logging.WARNING):
        rule_layers.cue_rules(bundle)
        rule_layers.cue_rules(bundle)
    assert caplog.text.count("more than one rule for github/mongo") == 1


def test_a_removed_option_is_reported_with_what_to_do_instead():
    (warning,) = [w for w in _warnings({"secrets": {"default": {"type": "vault"}}}) if "secrets" in w and "removed" in w]
    assert "secrets was removed and has no effect" in warning and "secret_managers" in warning


# ── What a sync would reject, ignore or still read ───────────────────────────

def _result(name: str, content) -> config_validator.Result:
    return config_validator.validate(name, content if isinstance(content, str) else json.dumps(content))


def test_a_vartrack_json_that_is_not_json_is_an_error():
    assert "not valid JSON" in _result("vartrack.json", "{").errors[0]


def test_a_file_that_is_not_a_settings_file_is_an_error():
    assert "not a varTrack settings file" in _result("app.yaml", "{}").errors[0]


def test_a_vartrack_json_under_a_platform_folder_is_checked_as_one():
    assert _result(".gitea/vartrack.json", {"branch_map": {"main": "production"}, "file_name": "app.yaml"}).errors == []


def test_a_vartrack_json_cannot_set_what_only_config_cue_sets():
    (warning,) = _result("vartrack.json", {"mongo_uri": "mongodb://x", "self_heal": True}).warnings
    assert "cannot set" in warning and "mongo_uri" in warning and "self_heal" in warning


def test_a_value_of_the_wrong_type_is_an_error():
    assert _result("vartrack.json", {"branch_map": ["main"]}).errors == ["vartrack.json: branch_map must be an object"]


def test_branch_is_an_option_of_a_bundle_rule():
    rule = {"platform": "github", "datasource": "mongo", "branch": "develop", "branch_map": {"main": "production"}, "file_name": "app.yaml"}
    assert _result("bundle.json", [rule]).errors == []
    bad = _result("bundle.json", [{**rule, "branch": 5}])
    assert bad.errors == ["bundle.json: the github/mongo rule: branch must be a string"]


def test_true_is_not_a_mode():
    rule = {"platform": "github", "datasource": "mongo", "sync_mode": True, "branch_map": {"main": "production"}, "file_name": "app.yaml"}
    assert _result("bundle.json", [rule]).errors == [
        "bundle.json: the github/mongo rule would be refused and not loaded: github/mongo: sync_mode true is not a mode: "
        "use AUTO, GIT_UPSERT_ALL or GIT_SMART_REPAIR"]


def test_a_bundle_rule_whose_sync_mode_was_removed_is_refused():
    rule = {"platform": "github", "datasource": "mongo", "sync_mode": "LIVE_STATE", "branch_map": {"main": "production"},
            "file_name": "app.yaml"}
    assert _result("bundle.json", [rule]).errors == [
        'bundle.json: the github/mongo rule would be refused and not loaded: github/mongo: sync_mode "LIVE_STATE" is '
        "not a mode: use AUTO, GIT_UPSERT_ALL or GIT_SMART_REPAIR"]
    assert _result("bundle.json", [{**rule, "sync_mode": "GIT_SMART_REPAIR"}]).errors == []


def test_max_changed_files_is_a_removed_option_a_warning_not_an_error():
    rule = {"platform": "github", "datasource": "mongo", "max_changed_files": 50, "branch_map": {"main": "production"}, "file_name": "app.yaml"}
    result = _result("bundle.json", [rule])
    assert result.errors == []
    [warning] = [w for w in result.warnings if "max_changed_files" in w]
    assert "max_changed_files was removed and has no effect" in warning and "files it names" in warning


def test_a_removed_option_is_a_warning_and_a_former_name_a_migration():
    result = _result("bundle.json", [{"platform": "github", "datasource": "mongo", "secrets": {}, "env_as_branch": True, "file_name": "app.yaml"}])
    assert result.errors == []
    assert any("secrets was removed" in w for w in result.warnings)
    assert any("env_as_branch is now branch_as_env" in m for m in result.migrations)


def test_two_rules_for_one_platform_and_datasource_are_an_error():
    rule = {"platform": "github", "datasource": "mongo"}
    assert "repeats github/mongo" in _result("bundle.json", [rule, rule]).errors[0]


# ── the tenant is where a file lives, never something it says ─────────────────

def test_a_bundle_json_rule_with_a_tenant_id_is_an_error_that_says_where_the_tenant_comes_from():
    result = config_validator.validate(
        "bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "tenant_id": "acme", "branch_map": {"main": "production"}, "file_name": "app.yaml"}]), "acme")
    assert result.errors == [
        "bundle.json: the gitea/mongo rule: tenant_id cannot be set — the tenant is the one whose schema "
        "repository holds this bundle.json (acme); delete the key"]


def test_a_tenant_id_that_names_another_tenant_says_so():
    result = config_validator.validate(
        "bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "tenant_id": "evil", "branch_map": {"main": "production"}, "file_name": "app.yaml"}]), "acme")
    [error] = result.errors
    assert "(it names tenant evil, which this file cannot act as)" in error


def test_every_rule_that_writes_it_is_named(monkeypatch):
    rules = [{"platform": "gitea", "datasource": "mongo", "tenant_id": "a", "branch_map": {"main": "production"}, "file_name": "app.yaml"},
             {"platform": "gitea", "datasource": "redis", "branch_map": {"main": "production"}, "file_name": "app.yaml"},
             {"platform": "gitea", "datasource": "s3", "tenant_id": "b", "branch_map": {"main": "production"}, "file_name": "app.yaml"}]
    result = config_validator.validate("bundle.json", json.dumps(rules), "acme")
    assert [e.split(":")[1].strip() for e in result.errors] == ["the gitea/mongo rule", "the gitea/s3 rule"]


def test_with_rules_in_config_cue_it_is_one_error_not_also_an_ignored_option(monkeypatch):
    bundle = {"rules": [{"platform": "gitea", "datasource": "mongo", "repositories": ["acme/*"]}]}
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: bundle)
    result = config_validator.validate(
        "bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "tenant_id": "acme", "branch_map": {"main": "production"}, "file_name": "app.yaml"}]), "acme")
    assert len(result.errors) == 1 and "tenant_id cannot be set" in result.errors[0]
    assert not any("tenant_id" in w for w in result.warnings)


def test_a_vartrack_json_with_a_tenant_id_is_an_error_not_a_warning():
    result = config_validator.validate("vartrack.json", json.dumps({"tenant_id": "evil", "prune": True}))
    assert result.errors == ["vartrack.json: tenant_id cannot be set — the tenant is the one that owns the "
                             "pushed repository; delete the key"]
    assert not any("tenant_id" in w for w in result.warnings)


def test_files_without_it_are_clean():
    assert config_validator.validate("vartrack.json", json.dumps({"prune": True})).errors == []
    clean = config_validator.validate(
        "bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "branch_map": {"main": "production"}, "file_name": "app.yaml"}]), "acme")
    assert clean.errors == []


# ── a rule that names no environments is an error: it would be refused and not loaded ──

def test_a_bundle_rule_that_names_no_environments_is_an_error_with_the_fix():
    result = config_validator.validate("bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "file_name": "app.yaml"}]), "acme")
    [error] = result.errors
    assert error.startswith("bundle.json: the gitea/mongo rule would be refused and not loaded: gitea/mongo names no environments")
    assert "branch_map" in error and 'branch_map: {"main": "default"}' in error


def test_a_bundle_rule_that_names_no_file_is_an_error_with_the_fix():
    result = config_validator.validate("bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "branch_map": {"main": "production"}}]), "acme")
    [error] = result.errors
    assert error.startswith("bundle.json: the gitea/mongo rule would be refused and not loaded: gitea/mongo names no file to read: set file_name")
    assert "file_path_map" in error and "A repository's vartrack.json can still change it" in error


def test_a_bundle_rule_that_names_neither_gets_one_error_naming_both():
    result = config_validator.validate("bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo"}]), "acme")
    [error] = result.errors
    assert "names no environments and no file to read" in error and "branch_map" in error and "file_name" in error


def test_a_bundle_rule_is_not_refused_for_what_config_cue_left_to_it(monkeypatch):
    bundle = {"rules": [{"platform": "gitea", "datasource": "mongo", "repositories": ["acme/*"]}]}
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: bundle)
    named = config_validator.validate(
        "bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "branch_map": {"develop": "staging"}, "file_name": "app.yaml"}]), "acme")
    assert named.errors == []
    unnamed = config_validator.validate("bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo"}]), "acme")
    assert len(unnamed.errors) == 1 and "would be refused and not loaded" in unnamed.errors[0]


def test_a_rule_whose_config_cue_names_the_environments_needs_nothing_more_of_bundle_json(monkeypatch):
    bundle = {"rules": [{"platform": "gitea", "datasource": "mongo", "repositories": ["acme/*"], "branch_as_env": True}]}
    monkeypatch.setattr("app.pipeline.datasource_settings._bundle", lambda: bundle)
    assert config_validator.validate("bundle.json", json.dumps([{"platform": "gitea", "datasource": "mongo", "file_name": "app.yaml"}]), "acme").errors == []
