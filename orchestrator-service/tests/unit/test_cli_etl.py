"""
The CLI's own pipeline steps: parsing a file's content (load_as), the ETL a
`vt sync` runs (run_stage_etl_content) and the check `vt validate` runs
(cli_ops.validate_content).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import cli_ops
from app.parsers.loader import load_as
from app.pipeline.models import PayloadContext
from app.pipeline.stage_etl import run_stage_etl_content


@pytest.mark.parametrize("content,path,fmt,want", [
    ("a: 1\nb:\n  c: x\n", "configs/app.yaml", None, {"a": 1, "b": {"c": "x"}}),
    ('{"a": 1}', "configs/app.json", None, {"a": 1}),
    ("A=1\nB=two\n", "app.env", None, {"A": "1", "B": "two"}),
    ("A=1\n", "settings.txt", "env", {"A": "1"}),       # the format named, not the extension
    ("a: 1\n", "config", "yml", {"a": 1}),
])
def test_load_as(content, path, fmt, want):
    assert load_as(content, path, fmt) == want


class _NoSchemas:
    def tenant_schema_root(self, tenant_id):
        return Path("/nonexistent/schemas") / tenant_id


def _ctx(rule):
    return PayloadContext(platform="gitea", repo_url="local", ref="refs/heads/cli/staging", branch="cli/staging",
                          commit_sha="cli-push", tag=None, pr_number=None, rule_config=rule, parsed_payload={})


def test_cli_etl_flattens_the_env_slice(monkeypatch):
    import app.schema_registry.manager as manager
    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _NoSchemas())

    content = "staging:\n  db: {host: stg.internal}\nproduction:\n  db: {host: prod.internal}\nmax_connections: 50\n"
    etl = run_stage_etl_content(ctx=_ctx({"root_key": ""}), file_path="configs/app.yaml", content=content,
                                fmt="yaml", env="staging", tenant_id="default")
    assert etl.errors == []
    assert len(etl.files) == 1
    f = etl.files[0]
    assert f.flat_data == {"db.host": "stg.internal", "max_connections": "50"}
    assert f.validation == "ok"


def test_cli_etl_takes_a_dotenv_file(monkeypatch):
    import app.schema_registry.manager as manager
    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _NoSchemas())

    etl = run_stage_etl_content(ctx=_ctx({}), file_path="configs/app.env", content="A=1\nB=two\n",
                                fmt="env", env="staging", tenant_id="default")
    assert etl.errors == []
    assert etl.files[0].flat_data == {"A": "1", "B": "two"}


def test_validate_content_reports_a_parse_error():
    status, messages, keys = cli_ops.validate_content(
        file_path="configs/app.json", content="{not json", fmt="json", datasource=None, tenant_id="default")
    assert status == "failed" and keys == 0
    assert messages[0].startswith("parse error")


def test_validate_content_without_a_schema_says_nothing_was_checked(monkeypatch):
    import app.schema_registry.manager as manager
    from app.pipeline import schema_utils
    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _NoSchemas())
    monkeypatch.setattr(schema_utils, "rule_config_for_datasource", lambda t, d, p="": {"root_key": "app"})

    status, messages, keys = cli_ops.validate_content(
        file_path="configs/app.yaml", content="app:\n  a: 1\n  b: 2\nother: x\n", fmt="yaml",
        datasource="mongo", tenant_id="default")
    assert status == "warn"
    assert keys == 2                      # only the rule's root_key subtree
    assert "no schema for app.yaml" in messages[-1]


def test_validate_content_uses_the_schema(monkeypatch, tmp_path):
    import app.schema_registry.manager as manager
    from app.pipeline import schema_utils, validator as validator_mod

    (tmp_path / "app.yaml.cue").write_text("max_connections: int\n")

    class _Schemas:
        def tenant_schema_root(self, tenant_id):
            return tmp_path

    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _Schemas())
    monkeypatch.setattr(schema_utils, "rule_config_for_datasource", lambda t, d, p="": None)
    monkeypatch.setattr(validator_mod.Validator, "_is_cue_available", lambda self: True)

    def fake_vet(self, flat, schema_path, file_name):
        return "" if flat.get("max_connections", "").isdigit() else "max_connections: conflicting values"

    monkeypatch.setattr(validator_mod.Validator, "_run_cue_vet", fake_vet)

    ok = cli_ops.validate_content(file_path="configs/app.yaml", content="max_connections: 50\n", fmt="yaml",
                                  datasource="mongo", tenant_id="default")
    bad = cli_ops.validate_content(file_path="configs/app.yaml", content="max_connections: lots\n", fmt="yaml",
                                   datasource="mongo", tenant_id="default")
    assert ok[0] == "ok"
    assert "no rule for datasource 'mongo'" in ok[1][0]
    assert bad[0] == "failed" and "conflicting values" in bad[1][-1]


def _run_cli_task(monkeypatch, lookup, **kw):
    from unittest.mock import MagicMock, patch

    from app.pipeline import schema_utils
    from app.tasks import etl

    monkeypatch.setattr(schema_utils, "rule_config_for_datasource", lookup)
    with patch.object(etl.process_cli_sync_task, "update_state"),          patch("app.monitoring.tracer.start_webhook_span", return_value=MagicMock()):
        return etl.process_cli_sync_task.run(
            datasource="mongo-primary", env="staging", file_path="a.yaml", content="a: 1", **kw)


def test_the_cli_task_looks_the_rule_up_by_the_platform_it_was_given(monkeypatch):
    seen = []

    def lookup(tenant, datasource, platform=""):
        seen.append((tenant, datasource, platform))
        return None

    result = _run_cli_task(monkeypatch, lookup, tenant_id="acme", platform="gitea-internal")

    assert seen == [("acme", "mongo-primary", "gitea-internal")]
    assert result["state"] == "FAILED"
    assert "platform 'gitea-internal' and datasource 'mongo-primary' not found" in result["message"]


def test_the_cli_task_fails_instead_of_guessing_between_platforms(monkeypatch):
    from app.pipeline import schema_utils

    def lookup(tenant, datasource, platform=""):
        raise schema_utils.AmbiguousRule("datasource 'mongo-primary' has rules on gitea, github: say which platform")

    result = _run_cli_task(monkeypatch, lookup)

    assert result["state"] == "FAILED" and "say which platform" in result["message"]
