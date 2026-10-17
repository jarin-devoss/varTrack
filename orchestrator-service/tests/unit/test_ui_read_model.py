"""
tests/unit/test_ui_read_model.py

The web UI read model: sink detection, desired vs current comparison,
drift classification, and reading values back from file-based sinks.
"""
from __future__ import annotations

import json
import threading

import pytest

from app.ui import ledger, views


def _rule(**kw):
    return {"platform": "github", "datasource": "redis", "branch_map": {"main": "production"}, **kw}


def _record(desired, *, secrets=None, env="production", task="t1"):
    return {
        "env": env,
        "datasource": "redis",
        "repository": "acme/configs",
        "ref": "main",
        "revision": "abc123",
        "desired": {**desired, **{k: ledger.SECRET_MASK for k in (secrets or {})}},
        "secretDigests": {k: ledger.secret_digest(v) for k, v in (secrets or {}).items()},
        "files": ["app.yaml"],
        "lastSync": {"taskId": task, "state": "SUCCEEDED", "trigger": "webhook", "startedAt": "2026-01-01T00:00:00Z"},
    }


@pytest.fixture(autouse=True)
def _no_cache_or_redis(monkeypatch):
    views._compare_cache.clear()
    views._compare_reading.clear()
    monkeypatch.setattr(views, "_drift_marker", lambda tenant, rule, env, differs: "2026-01-01T00:00:00Z" if differs else None)
    yield
    # Let background refreshes finish while the test's fake sink is still in place.
    for _ in range(250):
        if not views._compare_reading:
            break
        threading.Event().wait(0.02)


def _current(monkeypatch, values=None, error=None):
    monkeypatch.setattr(views, "_read_current", lambda rule, tenant, env, keys: (values, error))


class TestSinkType:
    @pytest.mark.parametrize("ds,expected", [
        ("mongo", "mongo"), ("mongo-primary", "mongo"), ("linux_server", "linux_server"),
        ("zookeeper", "zookeeper"), ("configmap", "configmap"),
    ])
    def test_from_datasource_name(self, ds, expected):
        assert views.sink_type({"datasource": ds}) == expected

    def test_from_connection_fields(self):
        assert views.sink_type({"datasource": "zk-main", "zk_hosts": ["zk:2181"]}) == "zookeeper"


class TestCompare:
    def test_matching_values_are_synced(self, monkeypatch):
        _current(monkeypatch, {"a": "1", "b": "2"})
        cmp = views.compare(_rule(), "acme", _record({"a": "1", "b": "2"}))
        assert cmp["differs"] == 0
        assert {k["key"]: k["state"] for k in cmp["keys"]} == {"a": "synced", "b": "synced"}

    def test_changed_and_missing_keys_differ(self, monkeypatch):
        _current(monkeypatch, {"a": "9"})
        cmp = views.compare(_rule(), "acme", _record({"a": "1", "b": "2"}))
        assert cmp["differs"] == 2
        states = {k["key"]: (k["state"], k.get("liveValue")) for k in cmp["keys"]}
        assert states == {"a": ("drifted", "9"), "b": ("missing", None)}

    def test_secrets_compare_by_digest_and_stay_masked(self, monkeypatch):
        _current(monkeypatch, {"db.password": "s3cret", "api.key": "rotated"})
        cmp = views.compare(_rule(), "acme", _record({}, secrets={"db.password": "s3cret", "api.key": "old"}))
        keys = {k["key"]: k for k in cmp["keys"]}
        assert keys["db.password"]["state"] == "synced"
        assert keys["api.key"]["state"] == "drifted"
        assert {keys["api.key"]["gitValue"], keys["api.key"]["liveValue"]} == {"***"}

    def test_protected_keys_follow_prune_protection(self, monkeypatch):
        _current(monkeypatch, {"SYSTEM_x": "1", "y": "2"})
        rule = _rule(prune_protection=["SYSTEM_*"])
        keys = {k["key"]: k["protected"] for k in views.compare(rule, "acme", _record({"SYSTEM_x": "1", "y": "2"}))["keys"]}
        assert keys == {"SYSTEM_x": True, "y": False}

    def test_compare_all_reads_every_sink_at_once(self, monkeypatch):
        rules = [_rule(datasource=ds) for ds in ("redis", "mongo", "linux_server")]
        records = [{**_record({"a": "1"}), "datasource": r["datasource"]} for r in rules]
        barrier = threading.Barrier(len(records), timeout=5)

        def read(rule, tenant, env, keys):
            barrier.wait()  # only passes when all three reads are in flight together
            return {"a": "1"}, None

        monkeypatch.setattr(views, "_read_current", read)
        views.compare_all(rules, "acme", records)

        # The views built afterwards are served from the cache.
        monkeypatch.setattr(views, "_read_current", lambda *a: pytest.fail("sink read twice"))
        assert [views.compare(rule, "acme", rec)["differs"] for rule, rec in zip(rules, records)] == [0, 0, 0]

    @pytest.fixture
    def slow_sink(self, monkeypatch):
        """The first read blocks until released; later reads answer at once."""
        reads, entered, release = [], threading.Event(), threading.Event()

        def read(rule, tenant, env, keys):
            reads.append(env)
            if len(reads) == 1:
                entered.set()
                release.wait(5)
            return {"a": "1"}, None

        monkeypatch.setattr(views, "_read_current", read)
        return reads, entered, release

    def test_concurrent_compares_share_one_sink_read(self, slow_sink):
        reads, entered, release = slow_sink
        results = []
        first = threading.Thread(target=lambda: results.append(views.compare(_rule(), "acme", _record({"a": "1"}))))
        first.start()
        entered.wait(5)
        others = [threading.Thread(target=lambda: results.append(views.compare(_rule(), "acme", _record({"a": "1"}))))
                  for _ in range(3)]
        for t in others:
            t.start()
        release.set()
        for t in [first, *others]:
            t.join(5)
        assert len(reads) == 1
        assert len(results) == 4 and all(r is results[0] for r in results)

    def test_a_fresh_compare_does_not_wait_for_an_earlier_read(self, slow_sink):
        reads, entered, release = slow_sink
        first = threading.Thread(target=lambda: views.compare(_rule(), "acme", _record({"a": "1"})))
        first.start()
        entered.wait(5)
        assert views.compare(_rule(), "acme", _record({"a": "1"}), fresh=True)["differs"] == 0
        assert len(reads) == 2
        release.set()
        first.join(5)

    def test_a_stale_comparison_is_answered_at_once_and_refreshed_behind(self, monkeypatch):
        _current(monkeypatch, {"a": "1"})
        views.compare(_rule(), "acme", _record({"a": "1"}))
        monkeypatch.setattr(views, "COMPARE_TTL_SECONDS", 0.0)

        refreshed, release = threading.Event(), threading.Event()

        def slow_read(rule, tenant, env, keys):
            release.wait(5)  # the sink is slow: the page must not wait for it
            refreshed.set()
            return {"a": "tampered"}, None

        monkeypatch.setattr(views, "_read_current", slow_read)
        assert views.compare(_rule(), "acme", _record({"a": "1"}))["differs"] == 0  # last comparison, at once
        release.set()
        assert refreshed.wait(5)
        key = ("acme", "redis", "production")
        for _ in range(250):  # the refresh lands in the cache just after the read
            if views._compare_cache[key][2]["differs"] == 1:
                break
            threading.Event().wait(0.02)
        else:
            pytest.fail("the background refresh never reached the cache")

    def test_a_new_sync_is_compared_again_before_answering(self, monkeypatch):
        _current(monkeypatch, {"a": "1"})
        views.compare(_rule(), "acme", _record({"a": "1"}, task="t1"))
        # The next sync wrote a=2: the comparison made against a=1 must not be reused.
        _current(monkeypatch, {"a": "2"})
        assert views.compare(_rule(), "acme", _record({"a": "2"}, task="t2"))["differs"] == 0


class TestTargetView:
    def test_in_sync(self, monkeypatch):
        _current(monkeypatch, {"a": "1"})
        t = views.target_view(_rule(destination_template="{env}:cfg"), "acme", _record({"a": "1"}), recent_tasks=[])
        assert (t["drift"], t["destination"], t["keyCount"]) == ("in_sync", "production:cfg", 1)

    def test_drifted(self, monkeypatch):
        _current(monkeypatch, {"a": "2"})
        t = views.target_view(_rule(), "acme", _record({"a": "1"}), recent_tasks=[])
        assert (t["drift"], t["driftedKeys"], t["driftDetectedAt"]) == ("drifted", 1, "2026-01-01T00:00:00Z")

    def test_healing_while_a_self_heal_runs(self, monkeypatch):
        _current(monkeypatch, {"a": "2"})
        running = {"taskId": "t2", "env": "production", "state": "RUNNING", "trigger": "self_heal", "createdAt": ledger.now_iso()}
        t = views.target_view(_rule(), "acme", _record({"a": "1"}), recent_tasks=[running])
        assert t["drift"] == "healing"
        assert t["lastSync"]["taskId"] == "t2"

    def test_heal_failed_after_drift(self, monkeypatch):
        _current(monkeypatch, {"a": "2"})
        failed = {"taskId": "t3", "env": "production", "state": "FAILED", "trigger": "self_heal",
                  "createdAt": "2026-01-01T00:01:00Z", "finishedAt": "2026-01-01T00:02:00Z", "message": "node is locked"}
        t = views.target_view(_rule(), "acme", _record({"a": "1"}), recent_tasks=[failed])
        assert (t["drift"], t["driftMessage"]) == ("heal_failed", "node is locked")

    def test_unknown_when_the_sink_cannot_be_read(self, monkeypatch):
        _current(monkeypatch, None, "can't connect to redis: refused")
        t = views.target_view(_rule(), "acme", _record({"a": "1"}), recent_tasks=[])
        assert (t["drift"], t["driftMessage"]) == ("unknown", "can't connect to redis: refused")


class TestRuleBase:
    def test_maps_bundle_fields(self):
        rule = _rule(repo_url="http://gitea:3000/acme/configs.git", sync_mode="SYNC_MODE_FULL", prune={"last": True},
                     file_path_map={"production": "configs/prod.yaml"}, self_heal=True)
        base = views.rule_base(rule, "acme")
        assert base["name"] == "github/redis"
        assert base["repositories"] == ["acme/configs"]
        assert base["syncMode"] == "GIT_UPSERT_ALL"
        assert base["prune"] == {"enabled": True, "last": True, "dryRun": False, "protection": []}
        assert base["filePathMap"] == {"configs/prod.yaml": "production"}
        assert base["selfHeal"] is True


def test_repo_name():
    assert ledger.repo_name("http://gitea:3000/acme/configs.git") == "acme/configs"
    assert ledger.repo_name("git@github.com:acme/configs.git") == "acme/configs"
    assert ledger.repo_name("acme/configs") == "acme/configs"


def test_trigger_from_headers():
    assert ledger.trigger_from_headers({"X-VarTrack-Trigger": "self_heal"}) == "self_heal"
    assert ledger.trigger_from_headers({"x-vartrack-trigger": "bogus"}) == "webhook"
    assert ledger.trigger_from_headers(None) == "webhook"


@pytest.mark.parametrize("fmt", ["env", "properties", "ini", "json", "yaml"])
def test_linux_server_parse_reads_back_what_render_writes(fmt):
    from app.pipeline.sinks.labels import VarTrackLabels
    from app.pipeline.sinks.linux_server import _parse, _render

    labels = VarTrackLabels.build(tenant_id="acme", datasource="linux_server", env="production",
                                  repo="acme/configs", branch="main", commit_sha="abc", file_path="app.yaml")
    data = {"db.host": "db.internal", "greeting": "hello world", "port": "5432"}
    assert _parse(_render(data, fmt, labels, "production"), fmt) == data


class TestReadValuesNeverFailsAWrite:
    """stage_sync reads @logger fields before every write for the change-log; that read must never fail the write."""

    def test_linux_server_file_that_no_longer_parses(self, tmp_path):
        from app.pipeline.sinks.linux_server import LinuxServerSink

        broken = tmp_path / "app.json"
        broken.write_text("{ not json", encoding="utf-8")
        sink = LinuxServerSink.__new__(LinuxServerSink)
        sink._ssh, sink._file_format, sink._resolve_path = None, "json", lambda env: broken
        assert sink.read_values(["db.host"], "linux_server", "production") == {}

    def test_configmap_api_unreachable(self):
        pytest.importorskip("kubernetes.client.exceptions")  # other tests stub a bare "kubernetes" module
        from app.pipeline.sinks.configmap import ConfigmapSink

        class Api:
            def read_namespaced_config_map(self, name, namespace):
                raise ConnectionError("connection refused")

        sink = ConfigmapSink.__new__(ConfigmapSink)
        sink._api, sink._resolve_cm_target = Api(), lambda env: ("default", "app-production")
        assert sink.read_values(["db.host"], "configmap", "production") == {}


class TestCheckDatasource:
    """What the watcher is told when a datasource changes."""

    @pytest.fixture
    def scene(self, monkeypatch):
        from app.ui import drift

        class Manager:
            def list_tenant_ids(self):
                return ["acme"]

            def get_or_clone(self, tenant):
                return None

            def get_all_rules(self, tenant):
                return [_rule(datasource="mongo"), _rule(datasource="redis")]

        state = {"tasks": [], "differs": {}}
        monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: Manager())
        monkeypatch.setattr(ledger, "list_targets", lambda tenant: [
            {**_record({"a": "1"}, env="production"), "datasource": "mongo"},
            {**_record({"a": "1"}, env="staging"), "datasource": "mongo"},
            {**_record({"a": "1"}, env="production"), "datasource": "redis"},
        ])
        monkeypatch.setattr(ledger, "list_tasks", lambda tenant, **kw: state["tasks"])
        monkeypatch.setattr(views, "compare", lambda rule, tenant, record, fresh=False: {
            "error": None, "keys": [], "differs": state["differs"].get(record["env"], 0)})
        return drift, state

    def test_in_sync_after_a_varTrack_write(self, scene):
        drift, _ = scene
        status, drifted, message = drift.check_datasource("mongo")
        assert (status, drifted) == ("in_sync", [])
        assert message == "2 environments match Git"

    def test_drifted_names_the_environment(self, scene):
        drift, state = scene
        state["differs"]["staging"] = 2
        assert drift.check_datasource("mongo")[:2] == ("drifted", ["github/mongo/staging"])

    def test_syncing_while_a_task_for_the_rule_runs(self, scene):
        drift, state = scene
        state["tasks"] = [{"rule": "github/mongo", "state": "RUNNING"}]  # env not known until it finishes
        assert drift.check_datasource("mongo")[0] == "syncing"
        state["tasks"] = [{"rule": "github/redis", "state": "RUNNING"}]  # another rule's sync doesn't count
        assert drift.check_datasource("mongo")[0] == "in_sync"

    def test_unknown_when_nothing_was_synced(self, scene):
        drift, _ = scene
        assert drift.check_datasource("zookeeper")[0] == "unknown"

    def test_a_task_abandoned_by_a_dead_worker_does_not_block_healing(self, scene):
        drift, state = scene
        state["differs"]["production"] = 1
        state["tasks"] = [{"rule": "github/mongo", "state": "RUNNING", "createdAt": "2020-01-01T00:00:00Z"}]
        assert drift.check_datasource("mongo")[0] == "drifted"

    def test_grpc_response_carries_the_status(self):
        # The stubs are generated, not committed: skip where they were not built.
        watcher_pb2 = pytest.importorskip("vartrack.v1.services.watcher_pb2")
        from app.grpc_server.server import _check_response

        resp = _check_response("drifted", ["github/mongo/staging"], "differs")
        assert resp.status == watcher_pb2.CheckDatasourceResponse.STATUS_DRIFTED
        assert list(resp.drifted) == ["github/mongo/staging"]


class TestAbandonedTasks:
    def test_is_active(self):
        now = 1_800_000_000.0
        started = ledger.now_iso(now - 60)
        assert ledger.is_active({"state": "RUNNING", "createdAt": started}, now)
        assert not ledger.is_active({"state": "SUCCEEDED", "createdAt": started}, now)
        long_ago = ledger.now_iso(now - ledger.TASK_STALE_AFTER_SECONDS - 1)
        assert not ledger.is_active({"state": "RUNNING", "createdAt": long_ago}, now)

    def test_the_ui_shows_an_abandoned_task_as_failed(self):
        task = views.public_task({"taskId": "t1", "state": "RUNNING", "createdAt": "2020-01-01T00:00:00Z", "_repoUrl": "x"})
        assert task["state"] == "FAILED" and "worker stopped" in task["message"]
        assert "_repoUrl" not in task


def test_a_finished_sync_records_what_it_wrote_before_it_says_it_finished(monkeypatch):
    """Otherwise the watcher can catch the gap: no sync running, the old desired state, and the new values."""
    from types import SimpleNamespace
    from app.ui import recorder

    calls = []
    monkeypatch.setattr(ledger, "record_task", lambda task, **kw: calls.append(("task", task["state"])))
    monkeypatch.setattr(ledger, "record_target", lambda **kw: calls.append(("target", kw["env"])))
    monkeypatch.setattr(ledger, "record_event", lambda **kw: None)

    task = {"taskId": "t1", "rule": "github/redis", "datasource": "redis", "trigger": "webhook", "createdAt": "x",
            "ref": "main", "revision": "abc", "_platform": "github", "_repoUrl": "r", "dryRun": False}
    result = SimpleNamespace(file_path="app.yaml", env="production", status="ok", sync_mode="", written=1, pruned=0,
                             root_key=None, error=None)
    etl = SimpleNamespace(files=[SimpleNamespace(file_path="app.yaml", env="production", flat_data={"a": "1"}, secret_fields=[])])
    recorder.task_finished(task, tenant="acme", etl=etl, results=[result])
    assert calls == [("target", "production"), ("task", "SUCCEEDED")]


def test_grpc_rule_lookup_loads_tenants_after_a_restart(monkeypatch):
    """Right after a restart nothing is cloned; a webhook must still find its rule."""
    import threading
    from app.pipeline import schema_utils

    class Entry:
        def __init__(self, rules):
            self.rules = rules

    class Manager:
        _lock = threading.Lock()

        def __init__(self):
            self._entries = {}
            self.cloned = []

        def list_tenant_ids(self):
            return ["default"]

        def get_or_clone(self, tenant):
            self.cloned.append(tenant)
            self._entries[f"{tenant}:schemas"] = Entry([{"platform": "gitea", "datasource": "mongo", "tenant_id": tenant}])

    mgr = Manager()
    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: mgr)
    schema_utils._reloaded_at.clear()
    assert schema_utils.rule_from_any_tenant("gitea", "mongo") == {"platform": "gitea", "datasource": "mongo", "tenant_id": "default"}
    assert mgr.cloned == ["default"]
    assert schema_utils.rule_from_any_tenant("gitea", "mongo") is not None
    assert mgr.cloned == ["default"]  # already loaded: no second clone
    assert schema_utils.rule_from_any_tenant("gitea", "nope") is None


def test_heal_for_one_datasource_resyncs_only_its_rules(monkeypatch):
    from app.tasks import etl

    dispatched = []
    monkeypatch.setattr(etl.process_webhook_task, "apply_async",
                        lambda kwargs, queue: dispatched.append((kwargs["datasource"], kwargs["raw_payload"])))

    class Extractor:
        def get_commit_sha(self, repo_url, branch, token=None):
            return f"sha-{branch}"

    monkeypatch.setattr("app.pipeline.git_extractor.GitExtractor", Extractor)
    rules = [
        {"platform": "gitea", "datasource": ds, "self_heal": True, "repo_url": "r",
         "branch_map": {"main": "production", "develop": "staging"}}
        for ds in ("mongo", "zookeeper", "configmap")
    ]

    etl.sync_all_task.run(rules=rules, datasource="mongo", platform="gitea")
    assert sorted(ds for ds, _ in dispatched) == ["mongo", "mongo"]
    assert {'"ref": "refs/heads/main"' in p or '"ref": "refs/heads/develop"' in p for _, p in dispatched} == {True}

    dispatched.clear()
    etl.sync_all_task.run(rules=rules)  # the periodic beat heals every self-heal rule
    assert len(dispatched) == 6


# ── Review fixes ──────────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.kv, self.sets = {}, {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, **kw):
        self.kv[k] = v

    def sadd(self, k, v):
        self.sets.setdefault(k, set()).add(v)


class TestDesiredStatePerFile:
    """A push syncs only the files it touched; the environment's other files still hold their keys."""

    KEY = "vt:ui:target:acme:redis:production"

    @pytest.fixture
    def fake(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(ledger, "_redis", lambda: fake)
        return fake

    def _record(self, fake, file_path, data, task, secrets=()):
        ledger.record_target(tenant="acme", platform="gitea", datasource="redis", env="production",
                             repo_url="r", ref="main", revision="abc", file_path=file_path, flat_data=data,
                             secret_fields=secrets, commit_message="", author="", sync={"taskId": task})
        return json.loads(fake.kv[self.KEY])

    def test_a_push_that_touches_one_file_keeps_the_others(self, fake):
        self._record(fake, "a.yaml", {"a": "1", "gone": "x"}, "t1")
        self._record(fake, "b.yaml", {"b": "2"}, "t2")
        doc = self._record(fake, "a.yaml", {"a": "9"}, "t3")  # "gone" was deleted from a.yaml in Git
        assert doc["desired"] == {"a": "9", "b": "2"}
        assert doc["files"] == ["a.yaml", "b.yaml"]

    def test_the_last_write_wins_for_a_key_in_two_files_and_secrets_follow_it(self, fake):
        self._record(fake, "a.yaml", {"k": "s3cret"}, "t1", secrets=("k",))
        doc = self._record(fake, "b.yaml", {"k": "plain"}, "t2")
        assert doc["desired"]["k"] == "plain" and "k" not in doc["secretDigests"]

    def test_a_record_from_before_per_file_tracking_is_carried_over(self, fake):
        fake.kv[self.KEY] = json.dumps({"files": ["a.yaml"], "desired": {"a": "1", "old": "x"}, "secretDigests": {}})
        doc = self._record(fake, "a.yaml", {"a": "2"}, "t9")
        assert doc["desired"] == {"a": "2"}  # the same single file: replaced, not merged


class TestRecorderAfterAFailedFile:
    """stage_sync stops at the first failing file; sinks that can roll back undo every earlier write."""

    @pytest.fixture
    def run(self, monkeypatch):
        from types import SimpleNamespace
        from app.ui import recorder

        calls = []
        monkeypatch.setattr(ledger, "record_task", lambda task, **kw: None)
        monkeypatch.setattr(ledger, "record_event", lambda **kw: calls.append(("event", kw["env"], kw["reason"])))
        monkeypatch.setattr(ledger, "record_target",
                            lambda **kw: calls.append(("target", kw["env"], kw["file_path"], kw["sync"]["state"])))
        monkeypatch.setattr(ledger, "update_target_sync", lambda **kw: calls.append(("failed", kw["env"])))

        def run(datasource, results):
            task = {"taskId": "t1", "rule": f"gitea/{datasource}", "datasource": datasource, "trigger": "webhook",
                    "createdAt": "x", "ref": "main", "revision": "abc", "_platform": "gitea", "_repoUrl": "r",
                    "dryRun": False}
            res = [SimpleNamespace(file_path=f, env=e, status=s, sync_mode="", written=int(s == "ok"), pruned=0,
                                   root_key=None, error=None if s == "ok" else "boom") for f, e, s in results]
            etl = SimpleNamespace(files=[SimpleNamespace(file_path=f, env=e, flat_data={"a": "1"}, secret_fields=[])
                                         for f, e, _ in results])
            recorder.task_finished(task, tenant="acme", etl=etl, results=res)
            return calls
        return run

    def test_a_sink_that_rolls_back_keeps_nothing_from_the_failed_sync(self, run):
        calls = run("mongo", [("prod.yaml", "production", "ok"), ("stg.yaml", "staging", "error")])
        assert not [c for c in calls if c[0] == "target"]
        assert ("failed", "production") in calls and ("failed", "staging") in calls
        assert ("event", "production", "SyncFailed") in calls

    def test_a_sink_without_rollback_keeps_the_files_written_before_the_failure(self, run):
        calls = run("redis", [("a.yaml", "production", "ok"), ("b.yaml", "production", "error")])
        assert ("target", "production", "a.yaml", "FAILED") in calls
        assert ("failed", "production") not in calls  # lastSync already carries the failure
        assert ("event", "production", "SyncFailed") in calls


def test_ui_resync_only_rereads_a_branch():
    from app.api.routers.ui import _branch_for
    rule = {"branch_map": {"main": "production"}}
    assert _branch_for(rule, "production", {"ref": "main"}) == "main"
    assert _branch_for(rule, "production", {"ref": "cli/production"}) == "main"
    assert _branch_for(rule, "production", {"ref": "refs/tags/v1.2"}) == "main"
    assert _branch_for({}, "production", {"ref": "cli/production"}) is None


def test_bundle_detail_masks_credentials_in_urls():
    from app.api.routers.ui import _public_detail
    detail = _public_detail({"mongo_uri": "mongodb://admin:hunter2@mongo:27017/db", "password": "x", "database": "vartrack"})
    assert detail == {"mongo_uri": "mongodb://***@mongo:27017/db", "database": "vartrack"}


class TestHelmReadBack:
    def test_values_set_with_set_read_back_as_written(self):
        from app.pipeline.sinks.helm import _flatten
        assert _flatten({"features": {"on": True, "off": False}, "n": 6000, "x": None}) == {
            "features.on": "true", "features.off": "false", "n": "6000", "x": "null"}

    def test_values_file_keys_get_their_prefix_back(self, tmp_path):
        from app.pipeline.sinks.helm import HelmSink
        from app.pipeline.sinks.strategies.helm.values_file import ValuesFileStrategy

        path = tmp_path / "values.yaml"
        path.write_text("db:\n  host: db.internal\n_vartrack:\n  tenant: acme\n", encoding="utf-8")
        sink = HelmSink.__new__(HelmSink)
        sink._strategy = ValuesFileStrategy(str(path), values_prefix="app")
        assert sink.read_values(["app.db.host", "db.host"], "helm", "production") == {"app.db.host": "db.internal"}


@pytest.mark.parametrize("rule,env,expected", [
    ({"datasource": "s3", "s3_bucket": "b", "s3_key_prefix": "configs"}, "production", "s3://b/configs/production/"),
    ({"datasource": "s3", "s3_bucket": "b"}, "production", "s3://b/production/"),
    ({"datasource": "linux_server", "file_path": "/etc/app/config.env"}, "feature/x", "/etc/app/config_feature_x.env"),
    ({"datasource": "linux_server", "file_path": "/etc/app/config.env", "env_as_dir": True}, "feature/x",
     "/etc/app/feature_x/config.env"),
])
def test_destination_is_where_the_sink_writes(rule, env, expected):
    assert views.destination(rule, "acme", env) == expected


def test_destination_template_keeps_its_placeholders():
    rule = {"datasource": "linux_server", "file_path": "/etc/app/config.env"}
    assert views.destination_template(rule) == "/etc/app/config_{env}.env"


def test_schema_clone_uses_the_tenants_branch(monkeypatch):
    from app.schema_registry import manager as m

    mgr = m.SchemaRegistryManager.__new__(m.SchemaRegistryManager)
    mgr._entries, mgr._lock = {}, threading.Lock()
    mgr._tenant_configs = {"acme": {"repo_url": "https://git.example.com/acme/schemas.git", "branch": "develop"}}
    cloned = []
    monkeypatch.setattr(m, "_validate_repo_url", lambda url: None)
    monkeypatch.setattr(mgr, "_clone", lambda tid, url, branch, token:
                        cloned.append(branch) or m._TenantEntry(tid, url, branch, local_path=None))
    mgr.get_or_clone("acme")
    assert cloned == ["develop"]


def test_unknown_datasource_webhooks_do_not_reload_schemas_every_time(monkeypatch):
    from app.pipeline import schema_utils

    class Manager:
        _lock = threading.Lock()
        _entries: dict = {}
        loads = 0

        def list_tenant_ids(self):
            return ["default"]

        def get_or_clone(self, tenant):
            Manager.loads += 1

    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: Manager())
    schema_utils._reloaded_at.clear()
    for _ in range(5):
        assert schema_utils.rule_from_any_tenant("gitea", "nope") is None
    assert Manager.loads == 1


def test_check_datasource_reads_environments_together(monkeypatch):
    from app.ui import drift

    class Manager:
        def list_tenant_ids(self):
            return ["acme"]

        def get_or_clone(self, tenant):
            pass

        def get_all_rules(self, tenant):
            return [_rule(datasource="mongo")]

    envs = ["production", "staging", "qa"]
    monkeypatch.setattr("app.schema_registry.manager.get_schema_manager", lambda: Manager())
    monkeypatch.setattr(ledger, "list_targets",
                        lambda t: [{**_record({"a": "1"}, env=e), "datasource": "mongo"} for e in envs])
    monkeypatch.setattr(ledger, "list_tasks", lambda t, **kw: [])
    barrier = threading.Barrier(len(envs), timeout=5)

    def compare(rule, tenant, record, fresh=False):
        barrier.wait()  # passes only when all three reads are in flight together
        return {"error": None, "keys": [], "differs": 1 if record["env"] == "qa" else 0}

    monkeypatch.setattr(views, "compare", compare)
    assert drift.check_datasource("mongo")[:2] == ("drifted", ["github/mongo/qa"])
