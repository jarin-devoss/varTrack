"""What the ETL does with the schema choice: which schema checks a file, what a file nothing checked reports, and
what never reaches the store (a `$schema` key, a placeholder a missing schema would have resolved)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.pipeline import validator as validator_mod
from app.pipeline.models import PayloadContext
from app.pipeline.stage_etl import run_stage_etl, run_stage_etl_content, take_schema_key

COMMIT = "c0ffee" * 6


class _Schemas:
    def __init__(self, root):
        self.root = root

    def current_schema_root(self, tenant_id):
        return self.root

    def schema_commit(self, tenant_id):
        return COMMIT


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "default.cue").write_text("{}\n")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "settings.cue").write_text('"db.password": string @secret()\n')
    return tmp_path


@pytest.fixture
def vet(monkeypatch):
    """cue is stood in for: every file passes, and the schemas it was asked to use are recorded."""
    used: list[str] = []
    monkeypatch.setattr(validator_mod.Validator, "_is_cue_available", lambda self: True)
    monkeypatch.setattr(validator_mod.Validator, "_run_cue_vet",
                        lambda self, flat, schema_path, file_name: used.append(schema_path.name) or None)
    return used


def ctx(rule):
    return PayloadContext(platform="gitea", repo_url="https://git/acme/configs", ref="refs/heads/main", branch="main",
                          commit_sha="abc", tag=None, pr_number=None,
                          rule_config={"platform": "gitea", "datasource": "mongo", "root_key": "", **rule},
                          parsed_payload={})


def sync_files(repo, files, rule):
    with (patch("app.pipeline.stage_etl.collect_files", return_value=files),
          patch("app.schema_registry.manager.get_schema_manager", return_value=_Schemas(repo))):
        return run_stage_etl(ctx(rule), datasource="mongo", tenant_id="acme")


# ── a file says what checked it ──────────────────────────────────────────────

def test_a_file_reports_the_schema_that_checked_it(repo, vet):
    rule = {"schema_map": [{"match": "services/**/*.yaml", "schema": "api/settings.cue"}]}
    with patch("app.pipeline.secret_resolver.resolve_secrets", side_effect=lambda flat, fields, secrets: flat):
        etl = sync_files(repo, [("services/api/app.yaml", "db:\n  password: x\n")], rule)
    [f] = etl.files
    assert (f.validation, f.schema, f.schema_source) == ("ok", "api/settings.cue", "schema_map[0]")
    assert vet == ["settings.cue"]
    assert etl.schema_commit == COMMIT


def test_secret_fields_come_from_the_mapped_schema(repo, vet):
    rule = {"schema_map": [{"match": "services/**/*.yaml", "schema": "api/settings.cue"}]}
    seen = {}

    def resolve(flat, fields, secrets):
        seen["fields"] = dict(fields)
        return {**flat, "db.password": "from-vault"}

    with patch("app.pipeline.secret_resolver.resolve_secrets", side_effect=resolve):
        etl = sync_files(repo, [("services/api/app.yaml", "db:\n  password: placeholder\n")], rule)
    assert seen["fields"] == {"db.password": None}
    assert etl.files[0].flat_data["db.password"] == "from-vault"
    assert etl.files[0].secret_fields == frozenset({"db.password"})


def test_a_file_nothing_checked_is_none_not_ok(tmp_path, vet):
    etl = sync_files(tmp_path, [("configs/app.yaml", "a: 1\n")], {})
    [f] = etl.files
    assert (f.validation, f.schema, f.schema_source) == ("none", "none", "none")
    assert vet == []


def test_the_schema_found_by_name_or_default_is_named(repo, vet):
    (repo / "app.yaml.cue").write_text("{}\n")
    etl = sync_files(repo, [("a/app.yaml", "a: 1\n"), ("a/other.yaml", "a: 1\n")], {})
    assert [(f.schema, f.schema_source) for f in etl.files] == [("app.yaml.cue", "name"), ("default.cue", "default")]


# ── a schema that cannot be honoured fails the file ──────────────────────────

def test_a_mapped_schema_that_is_missing_fails_the_file_even_without_strict(repo, vet):
    rule = {"schema_map": [{"match": "**/*.yaml", "schema": "nowhere.cue"}]}
    etl = sync_files(repo, [("a/app.yaml", "db:\n  password: placeholder\n")], rule)
    assert etl.files == []                      # nothing to write: the placeholder cannot reach a store
    assert etl.errors == ["a/app.yaml"]
    assert etl.failure("a/app.yaml") == (
        "schema nowhere.cue (schema_map[0], **/*.yaml) is not in the schema repository of tenant acme at c0ffeec0ffee")


def test_one_bad_file_does_not_stop_the_others(repo, vet):
    rule = {"schema_map": [{"match": "bad/*.yaml", "schema": "nowhere.cue"}]}
    etl = sync_files(repo, [("bad/a.yaml", "a: 1\n"), ("good/a.yaml", "a: 1\n")], rule)
    assert [f.file_path for f in etl.files] == ["good/a.yaml"] and etl.errors == ["bad/a.yaml"]


# ── strict_validation means every file is checked ────────────────────────────

def test_strict_validation_fails_a_file_no_schema_checks(tmp_path, vet):
    etl = sync_files(tmp_path, [("configs/app.yaml", "a: 1\n")], {"strict_validation": True})
    assert etl.files == [] and etl.errors == ["configs/app.yaml"]
    reason = etl.failure("configs/app.yaml")
    assert "strict_validation: no schema checks configs/app.yaml (rule gitea/mongo)" in reason
    assert "schema_map" in reason and '"none"' in reason


def test_strict_validation_lets_an_explicit_none_through(tmp_path, vet):
    rule = {"strict_validation": True, "schema_map": [{"match": "legacy/**", "schema": "none"}]}
    etl = sync_files(tmp_path, [("legacy/app.yaml", "a: 1\n")], rule)
    [f] = etl.files
    assert (f.validation, f.schema, f.schema_source) == ("none", "none", "schema_map[0]")


def test_strict_validation_still_fails_a_file_the_schema_rejects(repo, monkeypatch):
    monkeypatch.setattr(validator_mod.Validator, "_is_cue_available", lambda self: True)
    monkeypatch.setattr(validator_mod.Validator, "_run_cue_vet", lambda self, flat, path, name: "bad value")
    etl = sync_files(repo, [("a/app.yaml", "a: 1\n")], {"strict_validation": True})
    assert etl.errors == ["a/app.yaml"] and "strict_validation: CUE validation failed" in etl.failure("a/app.yaml")


def test_without_strict_a_rejected_file_is_warn_and_still_synced(repo, monkeypatch):
    monkeypatch.setattr(validator_mod.Validator, "_is_cue_available", lambda self: True)
    monkeypatch.setattr(validator_mod.Validator, "_run_cue_vet", lambda self, flat, path, name: "bad value")
    etl = sync_files(repo, [("a/app.yaml", "a: 1\n")], {})
    [f] = etl.files
    assert f.validation == "warn" and "bad value" in f.error


def test_a_missing_cue_program_marks_the_file_unchecked(repo, monkeypatch):
    monkeypatch.setattr(validator_mod.Validator, "_is_cue_available", lambda self: False)
    etl = sync_files(repo, [("a/app.yaml", "a: 1\n")], {})
    assert etl.files[0].validation == "none"
    strict = sync_files(repo, [("a/app.yaml", "a: 1\n")], {"strict_validation": True})
    assert strict.errors == ["a/app.yaml"]


# ── $schema in a data file ───────────────────────────────────────────────────

@pytest.mark.parametrize("name,content", [
    ("app.yaml", "$schema: https://json.schemastore.org/x.json\ndb:\n  host: h\n"),
    ("app.json", '{"$schema": "https://example/x.json", "db": {"host": "h"}}'),
    ("app.toml", '"$schema" = "https://example/x.json"\n[db]\nhost = "h"\n'),
])
def test_schema_is_never_synced_as_a_key(tmp_path, vet, name, content):
    etl = sync_files(tmp_path, [(f"configs/{name}", content)], {})
    assert etl.files[0].flat_data == {"db.host": "h"}


def test_take_schema_key_only_touches_tree_formats_and_the_top_level():
    nested = {"db": {"$schema": "x"}}
    assert take_schema_key(nested, "a.yaml") is None and nested == {"db": {"$schema": "x"}}
    env = {"$schema": "x"}
    assert take_schema_key(env, "a.env") is None and env == {"$schema": "x"}
    tree = {"$schema": "web.cue", "a": 1}
    assert take_schema_key(tree, "A.YAML") == "web.cue" and tree == {"a": 1}
    assert take_schema_key([1], "a.json") is None


def test_a_file_names_its_schema_only_when_the_rule_allows_it(repo, vet):
    (repo / "web.cue").write_text("{}\n")
    content = "$schema: web.cue\na: 1\n"
    off = sync_files(repo, [("x/app.yaml", content)], {})
    on = sync_files(repo, [("x/app.yaml", content)], {"schema_from_file": True})
    assert (off.files[0].schema, off.files[0].schema_source) == ("default.cue", "default")
    assert (on.files[0].schema, on.files[0].schema_source) == ("web.cue", "file")
    for etl in (off, on):
        assert "$schema" not in etl.files[0].flat_data


def test_the_map_outranks_the_file_and_the_note_travels_with_the_result(repo, vet):
    (repo / "web.cue").write_text("{}\n")
    rule = {"schema_from_file": True, "schema_map": [{"match": "x/*.yaml", "schema": "api/settings.cue"}]}
    with patch("app.pipeline.secret_resolver.resolve_secrets", side_effect=lambda flat, fields, secrets: flat):
        etl = sync_files(repo, [("x/app.yaml", "$schema: web.cue\ndb:\n  password: p\n")], rule)
    f = etl.files[0]
    assert (f.schema, f.schema_source) == ("api/settings.cue", "schema_map[0]")
    assert f.schema_note == "the file's $schema (web.cue) was overridden by schema_map[0]"


# ── the CLI's sync and validate see the same choice ──────────────────────────

def test_the_cli_sync_uses_the_map_too(repo, vet, monkeypatch):
    import app.schema_registry.manager as manager

    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _Schemas(repo))
    (repo / "web.cue").write_text("{}\n")
    rule = {"schema_map": [{"match": "configs/*.yaml", "schema": "web.cue"}]}
    etl = run_stage_etl_content(ctx=ctx(rule), file_path="configs/app.yaml", content="$schema: nope.cue\na: 1\n",
                                fmt="yaml", env="staging", tenant_id="acme")
    f = etl.files[0]
    assert (f.schema, f.schema_source, f.flat_data) == ("web.cue", "schema_map[0]", {"a": "1"})
    assert etl.schema_commit == COMMIT


def test_the_cli_sync_fails_a_mapped_schema_that_is_missing(repo, vet, monkeypatch):
    import app.schema_registry.manager as manager

    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _Schemas(repo))
    rule = {"schema_map": [{"match": "**/*.yaml", "schema": "nowhere.cue"}]}
    etl = run_stage_etl_content(ctx=ctx(rule), file_path="configs/app.yaml", content="a: 1\n",
                                fmt="yaml", env="staging", tenant_id="acme")
    assert etl.files == [] and "nowhere.cue" in etl.failure("configs/app.yaml")


def test_vt_validate_says_which_schema_checked_the_file(repo, vet, monkeypatch):
    import app.schema_registry.manager as manager
    from app import cli_ops
    from app.pipeline import schema_utils

    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _Schemas(repo))
    monkeypatch.setattr(schema_utils, "rule_config_for_datasource", lambda t, d, p="": {
        "schema_map": [{"match": "configs/*.yaml", "schema": "api/settings.cue"}]})
    monkeypatch.setattr("app.pipeline.secret_resolver.parse_secret_fields", lambda p: {})
    status, messages, _ = cli_ops.validate_content(file_path="configs/app.yaml", content="a: 1\n", fmt="yaml",
                                                   datasource="mongo", tenant_id="acme")
    assert status == "ok" and messages[-1] == "checked against schema api/settings.cue (schema_map[0])"


def test_vt_validate_fails_an_unchecked_file_under_strict_validation(tmp_path, vet, monkeypatch):
    import app.schema_registry.manager as manager
    from app import cli_ops
    from app.pipeline import schema_utils

    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _Schemas(tmp_path))
    monkeypatch.setattr(schema_utils, "rule_config_for_datasource", lambda t, d, p="": {"strict_validation": True})
    status, messages, _ = cli_ops.validate_content(file_path="configs/app.yaml", content="a: 1\n", fmt="yaml",
                                                   datasource="mongo", tenant_id="acme")
    assert status == "failed" and "no schema checks configs/app.yaml" in messages[-1]


def test_a_symlink_out_of_the_schema_repository_is_refused(repo, tmp_path_factory, vet):
    import os

    outside = tmp_path_factory.mktemp("outside") / "secret.cue"
    outside.write_text("{}\n")
    try:
        os.symlink(outside, repo / "link.cue")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    rule = {"schema_map": [{"match": "**/*.yaml", "schema": "link.cue"}]}
    etl = sync_files(repo, [("a/app.yaml", "a: 1\n")], rule)
    assert etl.files == [] and "leaves the schema repository" in etl.failure("a/app.yaml")
    assert Path(outside).exists()
