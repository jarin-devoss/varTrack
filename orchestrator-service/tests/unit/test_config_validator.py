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
                    {"prune": {"dry_run": True, "last": False}}):
        (warning,) = [w for w in _warnings(options) if "prune.last was removed" in w]
        assert "is disabled" not in warning


def test_a_prune_without_last_is_not_reported():
    assert _warnings({"prune": {"enabled": True, "protection": ["x"]}}) == []


@pytest.mark.parametrize("datasource,rule,effective", [
    ("s3", {}, False),
    ("configmap-app", {}, False),
    ("helm", {}, False),
    ("linux_server-web", {}, False),
    ("redis", {"redis_data_structure": "JSON"}, False),
    ("mongo-primary", {"update_strategy": "STRATEGY_FILE"}, False),
    ("mongo-primary", {"update_strategy": "STRATEGY_DOCUMENT"}, False),
    ("redis", {}, True),
    ("mongo", {"update_strategy": "STRATEGY_KEY_VALUE"}, True),
    ("zookeeper", {}, True),
    ("vercel", {}, True),
])
def test_prune_is_reported_only_where_the_sink_replaces_a_whole_object(datasource, rule, effective):
    result = config_validator.Result()
    config_validator._prune_without_effect({"prune": True, **rule}, datasource, "bundle.json: the rule", result)
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

    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: MagicMock(_tenant_configs={}))
    content = json.dumps({"rules": _rules(("github", "mongo"), ("github", "mongo"))})

    result = config_validator.validate("bundle.json", content)

    assert any("rule 2 repeats github/mongo (rule 1)" in e for e in result.errors)


def test_a_repeated_rule_in_config_cue_is_reported_too(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: MagicMock(_tenant_configs={}))
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
    assert _result(".gitea/vartrack.json", {"branch_map": {"main": "production"}}).errors == []


def test_a_vartrack_json_cannot_set_what_only_config_cue_sets():
    (warning,) = _result("vartrack.json", {"mongo_uri": "mongodb://x", "self_heal": True}).warnings
    assert "cannot set" in warning and "mongo_uri" in warning and "self_heal" in warning


def test_a_value_of_the_wrong_type_is_an_error():
    assert _result("vartrack.json", {"branch_map": ["main"]}).errors == ["vartrack.json: branch_map must be an object"]


def test_branch_and_max_changed_files_are_options_of_a_bundle_rule():
    rule = {"platform": "github", "datasource": "mongo", "branch": "develop", "max_changed_files": 50}
    assert _result("bundle.json", [rule]).errors == []
    bad = _result("bundle.json", [{**rule, "max_changed_files": "many"}])
    assert bad.errors == ["bundle.json: the github/mongo rule: max_changed_files must be a number"]


def test_true_is_not_a_number():
    rule = {"platform": "github", "datasource": "mongo", "max_changed_files": True}
    assert _result("bundle.json", [rule]).errors == [
        "bundle.json: the github/mongo rule: max_changed_files must be a number"]


def test_a_removed_option_is_a_warning_and_a_former_name_a_migration():
    result = _result("bundle.json", [{"platform": "github", "datasource": "mongo", "secrets": {}, "env_as_branch": True}])
    assert result.errors == []
    assert any("secrets was removed" in w for w in result.warnings)
    assert any("env_as_branch is now branch_as_env" in m for m in result.migrations)


def test_two_rules_for_one_platform_and_datasource_are_an_error():
    rule = {"platform": "github", "datasource": "mongo"}
    assert "repeats github/mongo" in _result("bundle.json", [rule, rule]).errors[0]
