"""
The CLI API's operations (app.cli_ops) and the gRPC CLIService that serves
them to the gateway.
"""
from __future__ import annotations

import pytest

from app import cli_ops, config_validator
from app.grpc_server import cli_servicer
from app.grpc_server.cli_servicer import CLIServicer

cli_pb2 = getattr(cli_servicer, "cli_pb2", None)

pytestmark = pytest.mark.skipif(not cli_servicer.CLI_PROTO_AVAILABLE, reason="proto stubs not generated")


class _Aborted(Exception):
    pass


class FakeContext:
    def __init__(self, **metadata: str) -> None:
        self._md = list(metadata.items())
        self.code = None

    def invocation_metadata(self):
        return self._md

    def abort(self, code, details):
        self.code = code
        raise _Aborted(details)


# ── cli_ops ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,hint,want", [
    ("configs/app.yaml", None, "yaml"),
    ("configs/app.YML", None, "yaml"),
    (".env", None, "yaml"),            # no extension Path knows → default
    ("app.env", None, "env"),
    ("app.toml", "JSON", "json"),      # an explicit format wins
])
def test_detect_format(path, hint, want):
    assert cli_ops.detect_format(path, hint) == want


def test_resolve_tenant_takes_the_first_non_blank():
    assert cli_ops.resolve_tenant(None, "  ", "acme") == "acme"
    assert cli_ops.resolve_tenant("", None) == "default"


def test_task_view_reads_the_sync_result_over_celery_success():
    # fail() returns normally, so Celery says SUCCESS; the result says FAILED.
    view = cli_ops._view("t1", None, {"state": "FAILED", "message": "etl errors: bad"}, "SUCCESS")
    assert view["state"] == "FAILED"
    assert view["error"] == "etl errors: bad"


def test_task_view_merges_the_ledger_record():
    record = {
        "state": "SUCCEEDED", "datasource": "mongo", "env": "staging", "dryRun": True, "_platform": "gitea-team",
        "revision": "release/1.2", "createdAt": "2026-10-18T09:00:00Z", "finishedAt": "2026-10-18T09:00:02Z",
        "files": [{"file": "configs/app.yaml"}],
    }
    result = {"state": "SUCCEEDED", "total_written": 19, "total_pruned": 2}
    view = cli_ops._view("t2", record, result, "SUCCESS")
    assert view == {
        "task_id": "t2", "tenant_id": "", "state": "SUCCEEDED", "message": "", "datasource": "mongo", "env": "staging",
        "file_path": "configs/app.yaml", "dry_run": True, "label": "release/1.2", "platform": "gitea-team",
        "written": 19, "pruned": 2, "created_at": "2026-10-18T09:00:00Z",
        "finished_at": "2026-10-18T09:00:02Z", "error": "",
    }


@pytest.mark.parametrize("celery_state,want", [
    ("PENDING", "PENDING"), ("STARTED", "RUNNING"), ("PROGRESS", "RUNNING"),
    ("RETRY", "RETRYING"), ("FAILURE", "FAILED"),
])
def test_task_view_maps_celery_states(celery_state, want):
    assert cli_ops._view("t", None, None, celery_state)["state"] == want


def test_list_tasks_keeps_only_cli_tasks(monkeypatch):
    from app.ui import ledger
    monkeypatch.setattr(ledger, "list_tasks", lambda tenant, limit: [
        {"taskId": "a", "trigger": "cli", "state": "SUCCEEDED", "datasource": "mongo"},
        {"taskId": "b", "trigger": "webhook", "state": "SUCCEEDED"},
        {"taskId": "c", "trigger": "cli", "state": "RUNNING"},
    ])
    assert [t["task_id"] for t in cli_ops.list_tasks("default", limit=5)] == ["a", "c"]
    assert [t["task_id"] for t in cli_ops.list_tasks("default", limit=1)] == ["a"]


# ── CLIService ───────────────────────────────────────────────────────────────

def test_sync_file_queues_the_task_as_the_caller(monkeypatch):
    from vartrack.v1.services import cli_pb2
    got = {}

    def fake_enqueue(**kw):
        got.update(kw)
        return "task-9"

    monkeypatch.setattr(cli_ops, "enqueue_sync", fake_enqueue)
    resp = CLIServicer().SyncFile(
        cli_pb2.SyncFileRequest(datasource="mongo", env="staging", file_path="configs/app.yml",
                                content="a: 1", dry_run=True, label="v1"),
        FakeContext(**{"x-cli-user": "bob@example.com", "x-tenant-id": "acme"}),
    )
    assert (resp.task_id, resp.dry_run) == ("task-9", True)
    assert got["submitted_by"] == "bob@example.com"
    assert got["tenant_id"] == "acme"
    assert got["fmt"] == "yaml"


def test_sync_file_names_missing_fields():
    from vartrack.v1.services import cli_pb2
    ctx = FakeContext()
    with pytest.raises(_Aborted, match="env, content"):
        CLIServicer().SyncFile(cli_pb2.SyncFileRequest(datasource="mongo", file_path="a.yaml"), ctx)
    assert ctx.code.name == "INVALID_ARGUMENT"


def test_validate_file(monkeypatch):
    from vartrack.v1.services import cli_pb2
    monkeypatch.setattr(cli_ops, "validate_content", lambda **kw: ("warn", ["schema missing"], 3))
    resp = CLIServicer().ValidateFile(
        cli_pb2.ValidateFileRequest(file_path="configs/app.yaml", content="a: 1"), FakeContext())
    assert (resp.status, list(resp.messages), resp.key_count) == ("warn", ["schema missing"], 3)


def test_get_task_converts_state_and_times(monkeypatch):
    from vartrack.v1.enums import task_state_pb2
    from vartrack.v1.services import cli_pb2
    monkeypatch.setattr(cli_ops, "task_view", lambda task_id: cli_ops._view(
        task_id, {"createdAt": "2026-10-18T09:00:00Z", "env": "staging"}, {"state": "SUCCEEDED", "total_written": 4}, "SUCCESS"))
    task = CLIServicer().GetTask(cli_pb2.GetCliTaskRequest(task_id="t1"), FakeContext())
    assert task.state == task_state_pb2.TASK_STATE_SUCCEEDED
    assert (task.env, task.written) == ("staging", 4)
    assert task.created_at.ToJsonString() == "2026-10-18T09:00:00Z"
    assert not task.HasField("finished_at")


def test_list_tasks_uses_the_metadata_tenant(monkeypatch):
    from vartrack.v1.services import cli_pb2
    seen = {}

    def fake_list(tenant, limit):
        seen.update(tenant=tenant, limit=limit)
        return [cli_ops._view("a", {"state": "RUNNING"}, None, "PENDING")]

    monkeypatch.setattr(cli_ops, "list_tasks", fake_list)
    resp = CLIServicer().ListTasks(cli_pb2.ListCliTasksRequest(limit=0), FakeContext(**{"x-tenant-id": "acme"}))
    assert seen == {"tenant": "acme", "limit": 20}
    assert [t.task_id for t in resp.tasks] == ["a"]
    assert resp.tasks[0].tenant_id == "acme"


def test_task_view_summarises_a_dry_run():
    result = {"state": "SUCCEEDED", "dry_run": True, "files": [
        {"file": "configs/app.yaml", "env": "staging", "keys": 19, "validation": "ok"},
        {"file": "configs/db.yaml", "env": "staging", "keys": 3, "validation": "warn"},
    ]}
    view = cli_ops._view("t", None, result, "SUCCESS")
    assert view["dry_run"] is True
    assert view["message"] == ("dry run — would write configs/app.yaml: 19 keys, validation ok; "
                               "configs/db.yaml: 3 keys, validation warn")


@pytest.mark.parametrize("rule,pushed,want", [
    ({"file_name": "app-settings.yaml"}, "configs/app-settings.yaml", "app-settings.yaml"),
    ({"file_name": "app-settings.yaml"}, r"C:\work\app-settings.yaml", "app-settings.yaml"),
    ({"file_name": "config/app.yaml"}, "config/app.yaml", "config/app.yaml"),
    ({"file_path_map": {"production": "configs/prod.yaml", "staging": "configs/stg.yaml"}}, "./stg.yaml", "configs/stg.yaml"),
    ({"file_name": "app-settings.yaml"}, "configs/other.yaml", "configs/other.yaml"),   # no match: kept
    ({}, "configs/app.yaml", "configs/app.yaml"),
])
def test_rule_file_for(rule, pushed, want):
    assert cli_ops.rule_file_for(rule, pushed) == want


def test_task_view_reports_the_tenant_the_task_ran_for():
    assert cli_ops._view("t", None, {"state": "FAILED", "message": "x", "tenant_id": "acme"}, "SUCCESS")["tenant_id"] == "acme"


# ── --revision: the rule's file as it is in Git at a branch or tag ───────────

@pytest.mark.parametrize("revision,ok", [
    ("v1.4.0", True), ("release/2026-10", True), ("main", True),
    ("--upload-pack=touch /tmp/x", False), ("-b", False), ("a..b", False),
    ("v1 ", False), ("x.lock", False), ("refs/", False), ("", False),
])
def test_valid_revision(revision, ok):
    assert cli_ops.valid_revision(revision) is ok


def test_file_at_revision_reads_the_env_s_file(monkeypatch):
    from app.pipeline import git_extractor
    seen = {}

    def extract(self, repo_url, ref, path, token=None):
        seen.update(repo_url=repo_url, ref=ref, path=path)
        return "a: 1"

    monkeypatch.setattr(git_extractor.GitExtractor, "extract_file", extract)
    rule = {"datasource": "redis", "repo_url": "http://gitea/acme/configs.git",
            "file_name": "app.yaml", "file_path_map": {"staging": "configs/stg.yaml"}}
    assert cli_ops.file_at_revision(rule, "staging", "v1.4.0") == ("configs/stg.yaml", "a: 1")
    assert seen == {"repo_url": "http://gitea/acme/configs.git", "ref": "v1.4.0", "path": "configs/stg.yaml"}
    assert cli_ops.file_at_revision(rule, "production", "v1.4.0")[0] == "app.yaml"

    monkeypatch.setattr(git_extractor.GitExtractor, "extract_file", lambda *a, **k: None)
    with pytest.raises(ValueError, match="not in .* at v9"):
        cli_ops.file_at_revision(rule, "production", "v9")
    with pytest.raises(ValueError, match="one repository"):
        cli_ops.file_at_revision({"datasource": "redis", "repositories": ["acme/*"], "file_name": "a.yaml"},
                                 "production", "v1")


def test_sync_file_with_a_revision_sends_no_file(monkeypatch):
    from vartrack.v1.services import cli_pb2
    got = {}
    monkeypatch.setattr(cli_ops, "enqueue_sync", lambda **kw: got.update(kw) or "task-1")
    CLIServicer().SyncFile(cli_pb2.SyncFileRequest(datasource="redis", env="production", revision="v1.4.0"),
                           FakeContext())
    assert got["revision"] == "v1.4.0"

    for request, match in [
        (cli_pb2.SyncFileRequest(datasource="redis", env="production", revision="v1", content="a: 1"), "send no file"),
        (cli_pb2.SyncFileRequest(datasource="redis", env="production", revision="-x"), "not a branch or tag"),
    ]:
        with pytest.raises(_Aborted, match=match):
            CLIServicer().SyncFile(request, FakeContext())


def test_sync_file_passes_the_rules_platform_on(monkeypatch):
    from vartrack.v1.services import cli_pb2
    got = {}
    monkeypatch.setattr(cli_ops, "enqueue_sync", lambda **kw: got.update(kw) or "task-1")
    CLIServicer().SyncFile(
        cli_pb2.SyncFileRequest(datasource="mongo-primary", env="staging", file_path="a.yaml", content="a: 1",
                                platform="gitea-internal"),
        FakeContext())
    assert got["platform"] == "gitea-internal"


def test_validate_file_passes_the_rules_platform_on(monkeypatch):
    from vartrack.v1.services import cli_pb2
    got = {}
    monkeypatch.setattr(cli_ops, "validate_content", lambda **kw: got.update(kw) or ("ok", [], 1))
    CLIServicer().ValidateFile(
        cli_pb2.ValidateFileRequest(file_path="a.yaml", content="a: 1", datasource="mongo-primary", platform="github"),
        FakeContext())
    assert got["platform"] == "github"


def test_validate_content_does_not_guess_between_platforms(monkeypatch):
    from app.pipeline import schema_utils

    def ambiguous(tenant, datasource, platform=""):
        raise schema_utils.AmbiguousRule("datasource 'mongo-primary' has rules on gitea, github: say which platform")

    monkeypatch.setattr(schema_utils, "rule_config_for_datasource", ambiguous)
    status, messages, keys = cli_ops.validate_content(
        file_path="configs/app.yaml", content="a: 1", fmt="yaml", datasource="mongo-primary", tenant_id="default")
    assert (status, keys) == ("failed", 0)
    assert "say which platform" in messages[0]


def test_task_view_names_the_platform_from_the_rule_when_the_record_has_no_platform():
    view = cli_ops._view("t", {"rule": "gitea-team/mongo"}, None, "SUCCESS")
    assert view["platform"] == "gitea-team"


# ── ValidateConfig ───────────────────────────────────────────────────────────

def test_validate_config_reports_what_the_validator_found(monkeypatch):
    seen = {}

    def fake(file_path, content, tenant_id):
        seen.update(file_path=file_path, tenant_id=tenant_id)
        result = config_validator.Result()
        result.errors.append("vartrack.json: prune must be an object or true/false")
        result.warnings.append("vartrack.json cannot set mongo_uri")
        result.migrations.append("vartrack.json: env_as_branch is now branch_as_env")
        return result

    monkeypatch.setattr(config_validator, "validate", fake)
    resp = CLIServicer().ValidateConfig(
        cli_pb2.ValidateConfigRequest(file_path=".gitea/vartrack.json", content="{}"),
        FakeContext(**{"x-tenant-id": "acme"}))
    assert list(resp.errors) == ["vartrack.json: prune must be an object or true/false"]
    assert list(resp.warnings) == ["vartrack.json cannot set mongo_uri"]
    assert list(resp.migrations) == ["vartrack.json: env_as_branch is now branch_as_env"]
    assert seen == {"file_path": ".gitea/vartrack.json", "tenant_id": "acme"}


def test_validate_config_names_missing_fields():
    ctx = FakeContext()
    with pytest.raises(_Aborted, match="file_path, content"):
        CLIServicer().ValidateConfig(cli_pb2.ValidateConfigRequest(file_path="vartrack.json"), ctx)
    assert ctx.code == cli_servicer.grpc.StatusCode.INVALID_ARGUMENT
