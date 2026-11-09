"""
A failed dependency is an error, never the value a read of nothing would give.

A push whose files cannot be parsed, a repository that cannot be cloned: the task is FAILED and says
which file or which repository and why, not SUCCEEDED with "no_files".
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import dependencies
from app.dependencies import SourceUnavailable
from app.pipeline.models import ETLFile, ETLResult
from app.tasks import etl


@pytest.fixture(autouse=True)
def _clean_registry():
    dependencies.reset()
    yield
    dependencies.reset()


def _ctx(rule=None):
    return MagicMock(repo_url="http://gitea/acme/configs.git", ref="refs/heads/main", branch="main",
                     commit_sha="abc", platform="github", rule_config=rule or {}, parsed_payload={})


def _run_task(etl_result=None, *, collect_error=None, dry_run=False, sync_results=None):
    ctx = _ctx()
    stage_etl = MagicMock(side_effect=collect_error) if collect_error else MagicMock(return_value=etl_result)
    with (
        patch.object(etl, "run_stage_payload", return_value=ctx),
        patch("app.pipeline.repo_overrides.apply_repo_overrides", side_effect=lambda c, t=None: c),
        patch.object(etl, "run_stage_etl", stage_etl),
        patch.object(etl, "run_stage_sync", return_value=sync_results or []),
        patch.object(etl, "ui_recorder"),
        patch.object(etl.process_webhook_task, "update_state"),
        patch("app.monitoring.tracer.start_webhook_span"),
    ):
        return etl.process_webhook_task.run(
            platform="github", datasource="mongo", raw_payload="{}", headers={}, received_at=0.0,
            rule_config={}, tenant_id="default", dry_run=dry_run)


def _errored(*paths, reason="ScannerError: mapping values are not allowed here"):
    result = ETLResult()
    for path in paths:
        result.errors.append(path)
        result.error_reasons[path] = reason
    return result


# ── P2: a file that does not parse ────────────────────────────────────────────

def test_invalid_yaml_is_a_failed_file_with_the_parse_error():
    from app.pipeline import stage_etl
    from app.pipeline.models import PayloadContext

    ctx = PayloadContext(platform="github", repo_url="http://gitea/acme/configs.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"file_name": "app.yaml", "branch_as_env": True}, parsed_payload={})
    with (
        patch.object(stage_etl, "collect_files", return_value=[("app.yaml", "a: b: c: [unclosed")]),
        patch("app.schema_registry.manager.get_schema_manager",
              return_value=MagicMock(current_schema_root=MagicMock(return_value=MagicMock(exists=lambda: False)))),
    ):
        result = stage_etl.run_stage_etl(ctx, "mongo", "default")
    assert result.files == [] and result.errors == ["app.yaml"]
    assert result.failure("app.yaml") != "could not be processed"


def test_a_push_whose_only_file_failed_is_not_no_files():
    out = _run_task(_errored("app.yaml"))
    assert out["state"] == "FAILED"
    assert out["message"] != "no_files"
    assert "app.yaml" in out["message"] and "ScannerError" in out["message"]
    assert out["files"] == [{"file": "app.yaml", "status": "error",
                             "error": "ScannerError: mapping values are not allowed here"}]


def test_a_dry_run_with_a_failed_file_fails_too():
    result = _errored("app.yaml")
    with patch("app.pipeline.stage_sync.prune_candidates", return_value={}):
        out = _run_task(result, dry_run=True)
    assert out["state"] == "FAILED" and out["files"][0]["status"] == "error"


def test_nothing_found_and_nothing_failed_is_still_no_files():
    out = _run_task(ETLResult())
    assert out["state"] == "SUCCEEDED" and out["message"] == "no_files"


def test_a_failed_file_next_to_a_written_one_fails_the_task_and_lists_both():
    from app.pipeline.models import SyncResult

    result = _errored("bad.yaml")
    result.files.append(ETLFile(file_path="ok.yaml", env="default", flat_data={"a": "1"}, root_key=""))
    sync = [SyncResult(file_path="ok.yaml", env="default", status="ok", written=1),
            SyncResult(file_path="bad.yaml", env="unknown", status="error", error="ScannerError: x")]
    out = _run_task(result, sync_results=sync)
    assert out["state"] == "FAILED" and out["errors"] == 1
    assert "bad.yaml: ScannerError: x" in out["message"]
    assert {f["file"]: f["status"] for f in out["files"]} == {"ok.yaml": "ok", "bad.yaml": "error"}


def test_every_file_synced_is_a_success():
    from app.pipeline.models import SyncResult

    result = ETLResult()
    result.files.append(ETLFile(file_path="ok.yaml", env="default", flat_data={"a": "1"}, root_key=""))
    out = _run_task(result, sync_results=[SyncResult(file_path="ok.yaml", env="default", status="ok", written=1)])
    assert out["state"] == "SUCCEEDED" and out["message"] == "completed"


# ── P15: a repository that cannot be cloned ───────────────────────────────────

def test_an_unreadable_repository_fails_the_task_naming_it():
    out = _run_task(collect_error=SourceUnavailable(
        "cannot read http://gitea/acme/configs.git at refs/heads/main: could not read Username"))
    assert out["state"] == "FAILED" and out["message"] != "no_files"
    assert out["message"].startswith("git unavailable: cannot read http://gitea/acme/configs.git")
    assert out["dependency"] == "git"


def test_a_password_never_reaches_the_message():
    out = _run_task(collect_error=SourceUnavailable("fatal: https://oauth2:hunter2@gitea/acme/configs.git denied"))
    assert "hunter2" not in out["message"] and "***" in out["message"]


def test_a_file_that_is_not_at_the_commit_is_still_just_absent():
    from app.tasks import file_collector

    extractor = MagicMock(extract_file=MagicMock(return_value=None))
    with patch.object(file_collector, "GitExtractor", return_value=extractor):
        got = file_collector.collect({}, "github", {"file_name": "app.yaml"}, "http://gitea/a/b.git", "refs/heads/main")
    assert got == []


def test_a_clone_failure_reaches_the_task_from_the_collector():
    from app.tasks import file_collector

    extractor = MagicMock(extract_file=MagicMock(side_effect=SourceUnavailable("cannot read x")))
    with patch.object(file_collector, "GitExtractor", return_value=extractor):
        with pytest.raises(SourceUnavailable):
            file_collector.collect({}, "github", {"file_name": "app.yaml"}, "http://gitea/a/b.git", "refs/heads/main")


# ── the credential a rule's repositories are read with ────────────────────────

def _platforms(monkeypatch, token="platform-token", endpoint="http://gitea:3000"):
    monkeypatch.setattr("app.pipeline.presets._platform", lambda name: (endpoint, token))


def test_a_rule_reads_its_repository_with_its_platforms_token(monkeypatch):
    from app.pipeline.git_credentials import credential_for

    _platforms(monkeypatch)
    assert credential_for({"platform": "github"}, "http://gitea:3000/acme/configs.git") == "platform-token"


def test_the_platform_token_is_never_sent_to_another_host(monkeypatch):
    from app.pipeline.git_credentials import credential_for

    _platforms(monkeypatch)
    # a wildcard rule takes the repository from the payload, which is the sender's claim
    assert credential_for({"platform": "github"}, "http://evil.example/acme/configs.git") is None


def test_a_rule_with_its_own_token_uses_it(monkeypatch):
    from app.pipeline.git_credentials import credential_for

    _platforms(monkeypatch)
    assert credential_for({"platform": "github", "token": "own"}, "http://evil.example/x.git") == "own"


def test_a_platform_that_is_not_in_config_cue_gives_an_anonymous_read(monkeypatch):
    from app.pipeline.git_credentials import credential_for
    from app.pipeline.presets import PresetError

    def missing(name):
        raise PresetError("no platform")

    monkeypatch.setattr("app.pipeline.presets._platform", missing)
    assert credential_for({"platform": "nope"}, "http://gitea:3000/a/b.git") is None


# ── a push with a failed file prunes nothing ──────────────────────────────────

def test_a_failed_file_stops_the_prune_of_the_push():
    from app.pipeline import stage_sync
    from app.pipeline.models import PayloadContext

    etl_result = _errored("bad.yaml")
    etl_result.files.append(ETLFile(file_path="ok.yaml", env="production", flat_data={"a": "1"}, root_key=""))
    sink = MagicMock()
    sink.snapshot_file_data.return_value = []
    sink.write.return_value = MagicMock(written=1, status="ok")
    ctx = PayloadContext(platform="github", repo_url="http://gitea/a/b.git", ref="refs/heads/main", branch="main",
                         commit_sha="abc", tag=None, pr_number=None, rule_config={"datasource": "redis"},
                         parsed_payload={})
    with (
        patch("app.pipeline.sinks.create_sink", return_value=sink),
        patch.object(stage_sync, "target_locks"),
        patch.object(stage_sync, "_prune_environment") as prune,
        patch.object(stage_sync, "_check_destinations"),
    ):
        results = stage_sync.run_stage_sync(ctx, etl_result, "redis", "default", 1)
    prune.assert_not_called()
    assert [(r.file_path, r.status) for r in results if r.file_path == "bad.yaml"] == [("bad.yaml", "error")]


# ── X1: a secret is the secret, or the file fails ─────────────────────────────

def test_vault_down_fails_the_file_and_nothing_of_it_is_written():
    from app.dependencies import SecretUnavailable
    from app.pipeline import stage_etl
    from app.pipeline.models import PayloadContext

    ctx = PayloadContext(platform="github", repo_url="http://gitea/acme/configs.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"file_name": "app.yaml", "branch_as_env": True}, parsed_payload={})
    unreachable = SecretUnavailable("database.password: cannot read vartrack/configs#database.password "
                                    "from secret manager 'default': vault unreachable")
    with (
        patch.object(stage_etl, "collect_files", return_value=[("app.yaml", "database:\n  password: placeholder\n")]),
        patch("app.pipeline.secret_resolver.resolve_secrets", side_effect=unreachable),
        patch("app.pipeline.secret_resolver.parse_secret_fields", return_value={"database.password": None}),
        patch("app.pipeline.secret_resolver.effective_secrets", return_value={"default": {}}),
        patch("app.pipeline.validator.Validator._find_schema", return_value=Path("app.yaml.cue")),
        patch("app.pipeline.change_logger.parse_logger_fields", return_value=frozenset()),
        patch("app.schema_registry.manager.get_schema_manager",
              return_value=MagicMock(current_schema_root=MagicMock(return_value=MagicMock(exists=lambda: False)))),
    ):
        result = stage_etl.run_stage_etl(ctx, "mongo", "default")
    assert result.files == []                       # nothing to write: the placeholder is not a secret
    assert result.errors == ["app.yaml"]
    assert "vault unreachable" in result.failure("app.yaml")


def test_a_secret_field_with_no_secret_manager_in_config_cue_fails_the_file():
    from app.pipeline import stage_etl
    from app.pipeline.models import PayloadContext

    ctx = PayloadContext(platform="github", repo_url="http://gitea/acme/configs.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"file_name": "app.yaml", "branch_as_env": True}, parsed_payload={})
    with (
        patch.object(stage_etl, "collect_files", return_value=[("app.yaml", "database:\n  password: placeholder\n")]),
        patch("app.pipeline.secret_resolver.parse_secret_fields", return_value={"database.password": None}),
        patch("app.pipeline.secret_resolver.effective_secrets", return_value={}),
        patch("app.pipeline.validator.Validator._find_schema", return_value=Path("app.yaml.cue")),
        patch("app.pipeline.change_logger.parse_logger_fields", return_value=frozenset()),
        patch("app.schema_registry.manager.get_schema_manager",
              return_value=MagicMock(current_schema_root=MagicMock(return_value=MagicMock(exists=lambda: False)))),
    ):
        result = stage_etl.run_stage_etl(ctx, "mongo", "default")
    assert result.files == [] and result.errors == ["app.yaml"]
    assert "no usable secret manager" in result.failure("app.yaml")
