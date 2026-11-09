"""
config.cue, a schema repository, the cue program: when one cannot be read, the answer says so.

Not "no rule for platform=github datasource=mongo" (true of an empty bundle, false of the operator's),
not an install that looks empty, not files written as if the tenant had no schemas.
"""
from __future__ import annotations

import pathlib
import threading
import time
from unittest.mock import MagicMock, patch

import grpc
import pytest
from httpx import ASGITransport, AsyncClient

from app import dependencies
from app.dependencies import BundleUnavailable, SchemaUnavailable


@pytest.fixture(autouse=True)
def _clean_registry():
    dependencies.reset()
    yield
    dependencies.reset()


def _app(mgr):
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=MagicMock()),
        patch("app.monitoring.init"),
        patch("app.monitoring.shutdown"),
    ):
        from app.api.app import create_app
        app = create_app()
    app.state.schema_manager = mgr
    return app


# ── the HTTP side ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_webhook_during_a_config_outage_is_503_naming_config_cue_not_no_rule():
    mgr = MagicMock()
    mgr.resolve_rule.side_effect = BundleUnavailable("vartrack-bundle failed: line 4: syntax error")
    app = _app(mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/v1/webhooks/mongo", content=b'{"repository":{"full_name":"a/b"}}',
                            headers={"x-github-event": "push"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["dependency"] == "config.cue" and "syntax error" in body["detail"]
    assert "no rule" not in body["detail"]


def test_the_ui_reads_the_bundle_through_an_error_not_an_empty_dict(monkeypatch):
    from app.api.routers import ui

    def unreadable(path, previous=None):
        raise BundleUnavailable("vartrack-bundle failed")

    monkeypatch.setattr("app.config.load_bundle", unreadable)
    monkeypatch.setattr("app.config.settings.CONFIG_PATH", "/etc/config.cue", raising=False)
    monkeypatch.setattr(ui, "_bundle_cache", None, raising=False)
    # what the UI reads the bundle through: an error, not {}
    with pytest.raises(BundleUnavailable):
        ui._cue_bundle()


def test_a_schema_failure_is_a_503_with_the_tenant_and_the_reason():
    import asyncio

    async def go():
        mgr = MagicMock()
        mgr.resolve_rule.side_effect = SchemaUnavailable("tenant demo: the schema repository could not be cloned")
        app = _app(mgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/v1/webhooks/mongo", content=b"{}", headers={"x-github-event": "push"})

    resp = asyncio.run(go())
    assert resp.status_code == 503 and "tenant demo" in resp.json()["detail"]


# ── the gRPC side ─────────────────────────────────────────────────────────────

def test_a_grpc_call_during_an_outage_answers_unavailable_and_names_it():
    from app.grpc_server.auth_interceptor import DependencyInterceptor

    def servicer(request, context):
        raise BundleUnavailable("vartrack-bundle failed")

    handler = grpc.unary_unary_rpc_method_handler(servicer)
    wrapped = DependencyInterceptor().intercept_service(lambda details: handler, MagicMock(method="/x.Y/Z"))
    context = MagicMock()
    wrapped.unary_unary(object(), context)
    context.abort.assert_called_once()
    code, message = context.abort.call_args.args
    assert code == grpc.StatusCode.UNAVAILABLE and message.startswith("config.cue unavailable:")


def test_an_ordinary_error_is_not_turned_into_unavailable():
    from app.grpc_server.auth_interceptor import DependencyInterceptor

    def servicer(request, context):
        raise KeyError("bug")

    wrapped = DependencyInterceptor().intercept_service(
        lambda details: grpc.unary_unary_rpc_method_handler(servicer), MagicMock(method="/x.Y/Z"))
    with pytest.raises(KeyError):
        wrapped.unary_unary(object(), MagicMock())


# ── reading config.cue while the service runs ─────────────────────────────────

def test_a_missing_config_file_on_the_first_read_is_an_error(monkeypatch, tmp_path):
    from app.pipeline import datasource_settings as ds

    monkeypatch.setattr("app.config.settings.CONFIG_PATH", str(tmp_path / "gone.cue"), raising=False)
    monkeypatch.setattr(ds, "_bundle_cache", None)
    with pytest.raises(BundleUnavailable, match="cannot be read"):
        ds._bundle()
    assert dependencies.snapshot()["config.cue"]["state"] == "unavailable"


def test_a_file_that_vanishes_later_keeps_the_last_good_bundle_and_says_stale(monkeypatch, tmp_path):
    from app.pipeline import datasource_settings as ds

    path = str(tmp_path / "config.cue")
    monkeypatch.setattr("app.config.settings.CONFIG_PATH", path, raising=False)
    monkeypatch.setattr(ds, "_bundle_cache", (path, 1.0, {"rules": [1]}))
    assert ds._bundle() == {"rules": [1]}
    assert dependencies.snapshot()["config.cue"]["state"] == "stale"


def test_no_config_path_at_all_is_an_empty_bundle_because_there_is_nothing_to_read(monkeypatch):
    from app.pipeline import datasource_settings as ds

    monkeypatch.setattr("app.config.settings.CONFIG_PATH", "", raising=False)
    assert ds._bundle() == {}


# ── a tenant's schema repository ──────────────────────────────────────────────

def _manager(monkeypatch, tmp_path):
    from app.config import settings
    from app.schema_registry import manager as manager_mod

    monkeypatch.setattr(settings, "SCHEMA_CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.utils.redis_cache.get_cache", lambda: None)
    monkeypatch.setattr(manager_mod, "_validate_repo_url", lambda url: None)
    mgr = manager_mod.SchemaRegistryManager.__new__(manager_mod.SchemaRegistryManager)
    mgr._root, mgr._entries, mgr._lock = tmp_path, {}, threading.Lock()
    mgr._tenant_configs, mgr._from_bundle = {"demo": {"repo_url": "https://git.example/demo/schemas.git",
                                                      "branch": "main", "token": None}}, set()
    monkeypatch.setattr(mgr, "sync_declared", lambda: None)
    return mgr, manager_mod


def test_a_schema_clone_that_fails_is_the_tenants_error_not_a_tenant_without_schemas(monkeypatch, tmp_path):
    mgr, manager_mod = _manager(monkeypatch, tmp_path)
    import git

    def refuse(url, path, **kw):
        raise git.GitCommandError("clone", 128, stderr="fatal: https://oauth2:hunter2@git.example denied")

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(refuse))
    mgr.get_or_clone("demo")

    reason = mgr.bundle_error("demo")
    assert "could not be cloned" in reason and "hunter2" not in reason
    with pytest.raises(SchemaUnavailable, match="tenant demo"):
        mgr.resolve_rule("github", "mongo", "demo")
    assert dependencies.snapshot()["schemas:demo"]["state"] == "unavailable"


def test_a_failed_clone_is_tried_again_once_it_is_stale(monkeypatch, tmp_path):
    mgr, manager_mod = _manager(monkeypatch, tmp_path)
    import git

    calls = []

    def flaky(url, path, **kw):
        calls.append(path)
        if len(calls) == 1:
            raise git.GitCommandError("clone", 128, stderr="network is unreachable")
        (manager_mod.Path(path) / "bundle.json").write_text("[]", encoding="utf-8")
        return MagicMock(head=MagicMock(commit=MagicMock(hexsha="abc")))

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(flaky))
    mgr.get_or_clone("demo")
    assert mgr.bundle_error("demo")
    mgr.get_or_clone("demo")                       # not stale yet: no second attempt
    assert len(calls) == 1
    for entry in mgr._entries.values():
        entry.fetched_at = time.time() - 10_000    # now stale
    mgr.get_or_clone("demo")
    assert len(calls) == 2 and mgr.bundle_error("demo") == ""
    assert dependencies.snapshot()["schemas:demo"]["state"] == "ok"


def test_a_refresh_that_fails_keeps_the_last_clone_and_says_stale(monkeypatch, tmp_path):
    mgr, manager_mod = _manager(monkeypatch, tmp_path)
    import git

    def fine(url, path, **kw):
        (manager_mod.Path(path) / "bundle.json").write_text("[]", encoding="utf-8")
        return MagicMock(head=MagicMock(commit=MagicMock(hexsha="abc")))

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(fine))
    mgr.get_or_clone("demo")
    for entry in mgr._entries.values():
        entry.fetched_at = time.time() - 10_000
    monkeypatch.setattr(mgr, "_refresh", MagicMock(side_effect=OSError("network is unreachable")))
    mgr.get_or_clone("demo")
    status = dependencies.snapshot()["schemas:demo"]
    assert status["state"] == "stale" and "network is unreachable" in status["reason"]
    assert mgr.bundle_error("demo") == ""          # the last good clone still serves


class _SharedCache:
    """The Redis every process of a deployment shares."""

    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ttl=300):
        self.values[key] = value

    def incr(self, key, ttl=300):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    def delete_pattern(self, pattern):
        pass


def test_a_schema_change_seen_by_one_process_refreshes_the_others_before_their_next_sync(monkeypatch, tmp_path):
    # The API (warm-up) or one worker (a push to the schema repository) invalidates; every other
    # worker of the pool holds its own clone, and must not validate the next sync against it.
    import git

    worker, manager_mod = _manager(monkeypatch, tmp_path / "worker")
    api, _ = _manager(monkeypatch, tmp_path / "api")
    shared = _SharedCache()
    monkeypatch.setattr("app.utils.redis_cache.get_cache", lambda: shared)

    def clone(url, path, **kw):
        (manager_mod.Path(path) / "bundle.json").write_text("[]", encoding="utf-8")
        return MagicMock(head=MagicMock(commit=MagicMock(hexsha="abc")))

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(clone))
    worker.get_or_clone("demo")
    refreshed = []
    monkeypatch.setattr(worker, "_refresh", lambda entry, token: refreshed.append(1) or entry)

    worker.current_schema_root("demo")
    assert refreshed == []                       # well inside the TTL, nothing changed: no fetch

    api.invalidate("demo")                       # the schemas changed, seen by another process
    worker.current_schema_root("demo")
    assert refreshed == [1]                      # this one fetches before it validates
    worker.current_schema_root("demo")
    assert refreshed == [1]                      # once


def _two_processes_and_a_cloning_git(monkeypatch, tmp_path):
    """An API-like and a worker-like manager over one shared Redis, a git whose every clone is a new commit."""
    import git

    worker, manager_mod = _manager(monkeypatch, tmp_path / "worker")
    api, _ = _manager(monkeypatch, tmp_path / "api")
    shared = _SharedCache()
    monkeypatch.setattr("app.utils.redis_cache.get_cache", lambda: shared)

    def clone(url, path, **kw):
        (manager_mod.Path(path) / "bundle.json").write_text("[]", encoding="utf-8")
        return MagicMock(head=MagicMock(commit=MagicMock(hexsha="c" * 40)))

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(clone))
    refreshed = []
    monkeypatch.setattr(worker, "_refresh", lambda entry, token: refreshed.append(1) or entry)
    return worker, api, shared, refreshed, manager_mod


def test_a_clock_that_steps_back_does_not_hide_a_schema_change_from_another_process(monkeypatch, tmp_path):
    """The generation was a wall-clock time, and a clone was stale when its generation was smaller than
    the published one. Docker Desktop's clock steps back by a fraction of a second now and then: the
    next warm-up then published a *smaller* number than the worker already held, the worker took itself
    for current, and a sync ran on the previous suite's schemas. A counter has no such failure."""
    worker, api, shared, refreshed, manager_mod = _two_processes_and_a_cloning_git(monkeypatch, tmp_path)
    clock = iter([2000.0, 2000.0, 2000.0, 2000.0, 1999.9, 1999.9, 1999.9, 1999.9] + [1999.9] * 50)
    monkeypatch.setattr(manager_mod.time, "time", lambda: next(clock))

    worker.get_or_clone("demo")
    api.invalidate("demo")                       # the schemas changed (clock reads 2000.0)
    worker.current_schema_root("demo")
    assert refreshed == [1]
    api.invalidate("demo")                       # and again, with the clock 100 ms earlier than before
    worker.current_schema_root("demo")
    assert refreshed == [1, 1]                   # the worker still sees it


def test_a_redis_that_lost_its_counter_makes_clones_fetch_again_not_trust_themselves(monkeypatch, tmp_path):
    worker, api, shared, refreshed, manager_mod = _two_processes_and_a_cloning_git(monkeypatch, tmp_path)
    api.invalidate("demo")
    api.invalidate("demo")
    worker.get_or_clone("demo")                  # fetched at generation 2
    shared.values.clear()                        # Redis restarted: the counter starts again
    api.invalidate("demo")                       # generation 1: lower than the worker's 2, and different
    worker.current_schema_root("demo")
    assert refreshed == [1]


def test_a_generation_that_could_not_be_published_is_said_and_the_ttl_still_applies(monkeypatch, tmp_path, caplog):
    import logging

    worker, api, shared, refreshed, manager_mod = _two_processes_and_a_cloning_git(monkeypatch, tmp_path)
    monkeypatch.setattr(shared, "incr", lambda key, ttl=300: None)     # Redis did not take it
    with caplog.at_level(logging.WARNING):
        api.invalidate("demo")
    assert "not published" in caplog.text and "demo" in caplog.text
    worker.get_or_clone("demo")
    worker.current_schema_root("demo")
    assert refreshed == []                       # nothing to see: the TTL decides, as it did before


def test_the_schema_commit_is_the_commit_of_the_clone_the_process_holds(monkeypatch, tmp_path):
    import git

    mgr, manager_mod = _manager(monkeypatch, tmp_path)
    shas = iter(["a" * 40, "b" * 40])

    def clone(url, path, **kw):
        (manager_mod.Path(path) / "bundle.json").write_text("[]", encoding="utf-8")
        return MagicMock(head=MagicMock(commit=MagicMock(hexsha=next(shas))))

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(clone))
    assert mgr.schema_commit("demo") == ""                 # nothing cloned yet
    mgr.get_or_clone("demo")
    assert mgr.schema_commit("demo") == "a" * 40
    mgr.invalidate("demo")
    assert mgr.schema_commit("demo") == ""                 # an invalidated clone is no clone
    mgr.get_or_clone("demo")
    assert mgr.schema_commit("demo") == "b" * 40


def test_a_clone_that_failed_has_no_schema_commit(monkeypatch, tmp_path):
    import git

    mgr, _ = _manager(monkeypatch, tmp_path)

    def refuse(url, path, **kw):
        raise git.GitCommandError("clone", 128, stderr="network is unreachable")

    monkeypatch.setattr(git.Repo, "clone_from", staticmethod(refuse))
    mgr.get_or_clone("demo")
    assert mgr.schema_commit("demo") == ""


def test_the_task_result_carries_the_schema_commit_in_each_outcome():
    from app.pipeline.models import ETLResult
    from app.tasks.etl import _build_dry_run_report

    etl = ETLResult()
    etl.schema_commit = "c0ffee"
    ctx = MagicMock(platform="gitea", branch="main", commit_sha="x" * 40, rule_config={}, repo_url="u")
    with patch("app.pipeline.stage_sync.prune_candidates", return_value={}):
        report = _build_dry_run_report("t1", ctx, etl, 0.0, "mongo", "demo")
    assert report["schema_commit"] == "c0ffee" and report["state"] == "SUCCEEDED"


def test_without_redis_a_clone_is_refreshed_on_its_ttl_as_before(monkeypatch, tmp_path):
    mgr, manager_mod = _manager(monkeypatch, tmp_path)
    entry = manager_mod._TenantEntry(tenant_id="demo", repo_url="u", branch="main", local_path=tmp_path)
    assert entry.is_stale() is False and entry.is_stale(manager_mod._published_generation("demo")) is False
    entry.fetched_at = time.time() - 10_000
    assert entry.is_stale() is True


# ── the lookups between the webhook and the rule ──────────────────────────────

def test_the_rule_lookup_lets_an_outage_through_instead_of_no_rule():
    from app.pipeline import schema_utils

    mgr = MagicMock()
    mgr.resolve_rule.side_effect = SchemaUnavailable("tenant demo: could not be cloned")
    with patch("app.schema_registry.manager.get_schema_manager", return_value=mgr):
        with pytest.raises(SchemaUnavailable):
            schema_utils.rule_from_bundle("github", "mongo", "demo")


def test_a_bug_in_the_lookup_is_still_no_rule_but_is_logged(caplog):
    import logging

    from app.pipeline import schema_utils

    mgr = MagicMock()
    mgr.resolve_rule.side_effect = KeyError("bug")
    with patch("app.schema_registry.manager.get_schema_manager", return_value=mgr), caplog.at_level(logging.ERROR):
        assert schema_utils.rule_from_bundle("github", "mongo", "demo") is None
    assert "rule lookup failed" in caplog.text


def test_a_heal_pass_that_cannot_read_config_cue_fails_instead_of_finding_no_rules():
    from app.pipeline import schema_utils

    mgr = MagicMock()
    mgr.list_tenant_ids.side_effect = BundleUnavailable("vartrack-bundle failed")
    with patch("app.schema_registry.manager.get_schema_manager", return_value=mgr):
        with pytest.raises(BundleUnavailable):
            schema_utils.rules_from_bundle_all()


def test_a_shallow_schema_clone_is_refreshed_after_the_remote_moved_on(tmp_path, monkeypatch):
    """The clone holds one commit of the branch; the remote then gets two more. A pull cannot reconcile
    that ("divergent branches"), and the tenant used to keep its first schemas for good."""
    import importlib
    import sys

    # tests/conftest.py stands a mock in for GitPython; this test needs the real one, with real repos
    for name in [n for n in sys.modules if n == "git" or n.startswith("git.")]:
        monkeypatch.delitem(sys.modules, name)
    try:
        git = importlib.import_module("git")
    except ImportError:
        pytest.skip("GitPython is not installed")
    monkeypatch.setitem(sys.modules, "git", git)

    import os
    import tempfile

    from app.schema_registry import manager as manager_mod

    if not str(tmp_path).isascii():
        # git on Windows trips over a non-ASCII user name in the temp path: use an ASCII one if there is one
        base = pathlib.Path(os.environ.get("SystemDrive", "C:") + "/tmp")
        if not base.is_dir():
            pytest.skip("no ASCII temporary directory for git")
        tmp_path = pathlib.Path(tempfile.mkdtemp(prefix="vt-schema-", dir=base))
    origin = tmp_path / "origin"
    origin.mkdir()
    work = git.Repo.init(str(origin), initial_branch="main")
    (origin / "bundle.json").write_text("[]", encoding="utf-8")
    work.index.add(["bundle.json"])
    work.index.commit("first")
    clone_dir = tmp_path / "clone"
    git.Repo.clone_from(origin.as_uri(), clone_dir, depth=1, single_branch=True, branch="main")

    for name in ("app-settings.yaml.cue", "other.cue"):        # the remote moves on, twice
        (origin / name).write_text("x: 1\n", encoding="utf-8")
        work.index.add([name])
        work.index.commit(f"add {name}")

    mgr = manager_mod.SchemaRegistryManager.__new__(manager_mod.SchemaRegistryManager)
    entry = manager_mod._TenantEntry(tenant_id="demo", repo_url=origin.as_uri(), branch="main", local_path=clone_dir)
    refreshed = mgr._refresh(entry, None)

    assert (clone_dir / "app-settings.yaml.cue").exists() and (clone_dir / "other.cue").exists()
    assert refreshed.commit_sha == work.head.commit.hexsha
