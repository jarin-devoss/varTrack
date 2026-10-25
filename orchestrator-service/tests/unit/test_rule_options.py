"""
rule.proto is the one list of rule options. The orchestrator derives what
merges, what only config.cue sets, what a vartrack.json may set, each option's
type and its former names from the proto's annotations (app.pipeline.rule_options).

These tests keep it that way: an option that is added to the proto has to be
documented and has to decide whether the web UI shows it, and a key the
orchestrator reads has to be an option — or say why it is not.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app import config_validator
from app.pipeline import rule_options
from app.pipeline.datasource_settings import _MAPPINGS
from app.ui import views

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs"


# ── What the annotations say ──────────────────────────────────────────────────

def test_the_options_are_the_ones_a_config_file_can_set():
    assert "last_applied_at" not in rule_options.FIELDS  # varTrack sets it
    assert {"platform", "datasource", "prune", "strict_validation", "suspend", "drift_ignore"} <= set(rule_options.FIELDS)


def test_what_merges_what_only_config_cue_sets_and_what_a_repo_may_set():
    assert set(rule_options.MERGEABLE) == {"branch_map", "file_path_map", "variables_map", "prune"}
    assert rule_options.ADMIN_ONLY == {
        "repositories", "exclude_repositories", "overrides", "secret_manager", "self_heal", "force", "tenants"}
    assert rule_options.REPO_OVERRIDABLE == {
        "branch_map", "dry_run", "branch_as_env", "pr_as_env", "tag_as_env", "file_name", "file_path_map",
        "prune", "root_key", "strict_validation", "sync_mode", "variables_map"}


def test_an_option_cannot_be_both_admin_only_and_open_to_a_repository():
    assert rule_options.ADMIN_ONLY.isdisjoint(rule_options.REPO_OVERRIDABLE)


def test_suspend_and_drift_ignore_are_neither_admin_only_nor_overridable_by_a_repo():
    # As they were before the lists came from the proto: a tenant's bundle.json may
    # layer them, a repository's vartrack.json may not.
    for name in ("suspend", "drift_ignore"):
        assert name not in rule_options.ADMIN_ONLY and name not in rule_options.REPO_OVERRIDABLE


def test_the_former_names_are_the_four_there_are():
    assert rule_options.RENAMED == {
        "env_as_branch": "branch_as_env",
        "env_as_pr": "pr_as_env",
        "env_as_tags": "tag_as_env",
        "env_as_prefix": "prefix_as_env",  # S3's, from its own proto
    }


@pytest.mark.parametrize("name,good,bad", [
    ("branch_map", {"main": "production"}, ["main"]),
    ("repositories", ["org/*"], "org/*"),
    ("self_heal", True, "yes"),
    ("file_name", "a.yaml", 3),
    ("prune", True, "yes"),
    ("prune", {"enabled": True}, 3),
    ("sync_mode", "AUTO", 1.5),
    ("sync_mode", 2, None),
    ("overrides", [{"enable": True}], {"enable": True}),
    ("secret_manager", "vault", ["vault"]),
    ("strict_validation", True, "true"),
])
def test_each_option_takes_the_json_type_its_proto_field_has(name, good, bad):
    assert isinstance(good, rule_options.TYPES[name])
    assert not isinstance(bad, rule_options.TYPES[name])


def test_the_validator_checks_every_option_not_only_the_ones_it_once_listed():
    # overrides, platform, datasource and secret_manager were not in its list.
    result = config_validator.Result()
    config_validator._types({"overrides": "x", "secret_manager": 3, "sync_mode": 2.5}, "bundle.json", result)
    assert sorted(result.errors) == [
        "bundle.json: overrides must be a list",
        "bundle.json: secret_manager must be a string",
        "bundle.json: sync_mode must be a string or a number",
    ]


def test_branch_and_max_changed_files_are_options_neither_admin_only_nor_open_to_a_repo():
    # Both were read from a rule and declared nowhere, so the gateway's strict
    # loader rejected them in config.cue.
    for name in ("branch", "max_changed_files"):
        assert name in rule_options.FIELDS
        assert name not in rule_options.ADMIN_ONLY and name not in rule_options.REPO_OVERRIDABLE
    assert isinstance(500, rule_options.TYPES["max_changed_files"])
    assert not isinstance("500", rule_options.TYPES["max_changed_files"])


def test_strict_validation_is_a_rule_option_a_repo_may_set():
    # It was read by the orchestrator and set in vartrack.json, but was not a
    # field, so the gateway's strict loader rejected it in config.cue.
    assert "strict_validation" in rule_options.FIELDS
    assert "strict_validation" in rule_options.REPO_OVERRIDABLE


# ── Every key the orchestrator reads is an option, or says why not ────────────

# Keys the orchestrator reads from a rule that are not options in rule.proto.
# A new key does not belong here: it is a proto field. These are the ones that
# are not, and why.
NOT_OPTIONS: dict[str, str] = {
    "repo_url": "filled in when the rule is built (the clone URL of its repository)",
    "tenant_id": "filled in when the rule is built (the tenant it was loaded for)",
    "token": "filled in when the rule is built (the platform's token)",
    "extends": "preset syntax, resolved before the rule is built",
    "dry_run_prune": "the former flat name of prune.dry_run, still read",
    "prune_protection": "the former flat name of prune.protection, still read",
    # Sink options a tenant's bundle.json sets and config.cue has no field for
    # (docs/configuration/schema-registry.md: "sink options with no config.cue field").
    "helm_binary": "a Helm sink option, set in bundle.json only",
    "helm_values_file_path": "a Helm sink option, set in bundle.json only",
    "helm_values_mode": "a Helm sink option, set in bundle.json only",
    "zk_path_template": "the ZooKeeper sink's former name for destination_template, still read",
    "env_as_filename": "a Linux-server sink option, set in bundle.json only",
}

_RECEIVERS = {"rule_config", "rule"}


def _rule_keys_read() -> dict[str, set[str]]:
    """Every literal rule_config.get("key") / rule_config["key"] in the pipeline,
    the tasks, the web UI's read model and the CLI operations, with its files."""
    files = [*APP.glob("pipeline/**/*.py"), *APP.glob("tasks/*.py"), *APP.glob("ui/*.py"), APP / "cli_ops.py"]
    keys: dict[str, set[str]] = {}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            key = None
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in _RECEIVERS
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                key = node.args[0].value
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                  and node.value.id in _RECEIVERS and isinstance(node.slice, ast.Constant)
                  and isinstance(node.slice.value, str)):
                key = node.slice.value
            if key:
                keys.setdefault(key, set()).add(path.name)
    return keys


def test_every_rule_key_the_orchestrator_reads_is_an_option_or_says_why_not():
    datasource_keys = {key for mapping in _MAPPINGS.values() for key, _ in mapping}
    undeclared = {
        key: sorted(files) for key, files in _rule_keys_read().items()
        if key not in rule_options.FIELDS and key not in datasource_keys and key not in NOT_OPTIONS
    }
    assert not undeclared, (
        f"read from a rule but not an option of rule.proto: {undeclared}. Add the option to rule.proto "
        "(strict_validation and secrets were read like this and were never fields); only a key that "
        "cannot be one belongs in NOT_OPTIONS, with the reason")


def test_nothing_on_the_not_options_list_has_become_an_option_or_is_no_longer_read():
    read = _rule_keys_read()
    stale = sorted(k for k in NOT_OPTIONS if k in rule_options.FIELDS or k not in read)
    assert not stale, f"take these off NOT_OPTIONS: {stale}"


# ── Every option is documented ────────────────────────────────────────────────

def test_every_option_is_documented():
    text = "\n".join(
        (DOCS / "configuration" / name).read_text(encoding="utf-8")
        for name in ("bundle.md", "schema-registry.md", "per-repo-overrides.md")
    )
    missing = sorted(n for n in rule_options.FIELDS if not re.search(r"\b" + re.escape(n) + r"\b", text))
    assert not missing, f"not in bundle.md, schema-registry.md or per-repo-overrides.md: {missing}"


def test_the_former_names_are_documented_too():
    text = (DOCS / "concepts" / "environment-resolution.md").read_text(encoding="utf-8")
    missing = sorted(old for old in rule_options.RENAMED if old not in text)
    assert not missing, f"not in environment-resolution.md: {missing}"


# ── Every option is in the web UI's rule view, or says why not ────────────────

def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def test_every_option_is_in_the_rule_view_or_says_why_not():
    shown = views.rule_base(
        {"platform": "github", "datasource": "mongo", "file_name": "a.yaml", "secret_manager": "vault"}, "acme")
    missing = sorted(
        name for name in rule_options.FIELDS
        if views.UI_NAMES.get(name, _camel(name)) not in shown and name not in views.NOT_SHOWN_IN_UI
    )
    assert not missing, (
        f"not in rule_base's output and not in views.NOT_SHOWN_IN_UI: {missing}. "
        "Show it on the rule page, or say in NOT_SHOWN_IN_UI why not")


def test_nothing_in_not_shown_is_shown_or_not_an_option():
    shown = views.rule_base(
        {"platform": "github", "datasource": "mongo", "file_name": "a.yaml", "secret_manager": "vault"}, "acme")
    stale = sorted(
        n for n in views.NOT_SHOWN_IN_UI
        if n not in rule_options.FIELDS or views.UI_NAMES.get(n, _camel(n)) in shown
    )
    assert not stale, f"take these off NOT_SHOWN_IN_UI: {stale}"
