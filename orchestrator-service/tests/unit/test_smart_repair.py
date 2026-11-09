"""GIT_SMART_REPAIR on every sink: the store is read, and only what differs is written.

A per-key store (Redis, ZooKeeper, Mongo key-value) writes the keys that differ; a store that holds the file as one
object (a Mongo document, an S3 object, a file, a ConfigMap, a values file or release) is left alone when it holds the
file and written whole when it does not. Content is compared, never the commit that stamped it. When the store cannot
be read, every key is written, and the file's result says so.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.pipeline.sinks.base import UNREADABLE, differs
from app.pipeline.sinks.settings import Settings
from app.utils.enums.sync_mode import SyncMode
from app.pipeline.sinks.key_formatter import key_format_from_str
from tests.unit.test_sink_commits import (
    LABELS, FakeRedis, FakeZk, NoAuthError, NodeExistsError, NoNodeError, RolledBackError, redis_sink,
)

SMART = SyncMode.GIT_SMART_REPAIR
UPSERT = SyncMode.GIT_UPSERT_ALL
WRITE = dict(datasource="ds", env="prod", repo="r", branch="main", commit_sha="abc", file_path="app.yaml",
             total_sources=1)
FIFTY = {f"k{i:02d}": str(i) for i in range(50)}


# ── the comparison ────────────────────────────────────────────────────────────

def test_differs_names_the_keys_the_store_does_not_hold_as_git_has_them():
    write, unchanged = differs({"a": "1", "b": "old"}, {"a": "1", "b": "new", "c": "3"})
    assert write == {"b": "new", "c": "3"} and unchanged == 1


def test_a_value_the_store_never_gives_back_is_written():
    assert differs({"secret": UNREADABLE}, {"secret": "x"}) == ({"secret": "x"}, 0)


def test_values_are_compared_as_text():
    assert differs({"port": "5432"}, {"port": 5432}) == ({}, 1)


# ── Redis ─────────────────────────────────────────────────────────────────────

class ReadableRedis(FakeRedis):
    def hmget(self, key, fields):
        held = self.data.get(key, {})
        return [held.get(f) for f in fields]

    def mget(self, keys):
        return [self.data.get(k) for k in keys]

    def get(self, key):
        return self.data.get(key)


def _redis_with(values):
    store = ReadableRedis()
    store.data["acme:ds:prod:vartrack"] = dict(values)
    return store


def test_redis_smart_repair_writes_the_one_key_that_changed_and_the_stamp():
    store = _redis_with(FIFTY)
    desired = {**FIFTY, "k07": "changed"}
    out = redis_sink(store)._write(flat_data=desired, sync_mode=SMART, labels=LABELS, **WRITE)
    [pipe] = store.pipelines
    assert out == {"written": 1, "unchanged": 49}
    assert pipe.queued[0] == ("hset", "acme:ds:prod:vartrack", {"k07": "changed"})
    assert [c[1] for c in pipe.queued] == ["acme:ds:prod:vartrack", "acme:ds:prod:__vartrack__"]


def test_redis_smart_repair_with_nothing_different_runs_no_transaction():
    store = _redis_with(FIFTY)
    out = redis_sink(store)._write(flat_data=FIFTY, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out == {"written": 0, "unchanged": 50} and store.pipelines == []


def test_redis_upsert_all_writes_every_key_whatever_the_store_holds():
    store = _redis_with(FIFTY)
    out = redis_sink(store)._write(flat_data=FIFTY, sync_mode=UPSERT, labels=LABELS, **WRITE)
    assert out == {"written": 50, "unchanged": 0}
    assert len(store.pipelines[0].queued[0][2]) == 50


def test_redis_strings_write_only_the_keys_that_differ():
    store = ReadableRedis()
    store.data.update({"acme:ds:prod:a": "1", "acme:ds:prod:b": "old"})
    out = redis_sink(store, structure="STRING")._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART,
                                                       labels=LABELS, **WRITE)
    assert out == {"written": 1, "unchanged": 1}
    assert [c[:2] for c in store.pipelines[0].queued] == [("set", "acme:ds:prod:b"), ("hset", "acme:ds:prod:__vartrack__")]


def test_the_redis_json_string_is_written_whole_or_not_at_all():
    store = ReadableRedis()
    store.data["acme:ds:prod:json"] = json.dumps({"a": "1", "b": "2"})
    same = redis_sink(store, structure="JSON")._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert same == {"written": 0, "unchanged": 2} and store.pipelines == []
    other = redis_sink(store, structure="JSON")._write(flat_data={"a": "1", "b": "3"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert other["written"] == 1 and other["unchanged"] == 0


def test_redis_that_cannot_be_read_gets_every_key_and_says_so():
    store = ReadableRedis()
    store.hmget = MagicMock(side_effect=ConnectionError("redis went away"))
    out = redis_sink(store)._write(flat_data=FIFTY, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out["written"] == 50 and out["unchanged"] == 0
    assert out["sync_mode"] == "GIT_UPSERT_ALL" and "redis went away" in out["note"] and "wrote every key" in out["note"]


# ── ZooKeeper ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def kazoo_errors(monkeypatch):
    """tests/conftest.py stands a mock in for kazoo.exceptions; the sink needs classes it can isinstance()."""
    import sys
    import types

    module = types.ModuleType("kazoo.exceptions")
    for cls in (NoNodeError, NodeExistsError, RolledBackError, NoAuthError):
        setattr(module, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, "kazoo.exceptions", module)


@pytest.fixture
def zk_sink():
    from app.pipeline.sinks.zookeeper import ZookeeperSink

    def make(zk):
        sink = ZookeeperSink.__new__(ZookeeperSink)
        sink._zk, sink._base_path, sink._tenant_id = zk, "", "acme"
        sink._path_template, sink._key_format = "/{tenant}/{env}", key_format_from_str("slash")
        return sink
    return make


class ReadableZk(FakeZk):
    def get(self, path):
        return self.nodes[path], None


def _zk_with(values):
    return ReadableZk({f"/acme/prod/{k}": v.encode() for k, v in values.items()})


def test_zookeeper_smart_repair_writes_the_one_znode_that_changed_and_the_stamp(zk_sink):
    zk = _zk_with(FIFTY)
    out = zk_sink(zk)._write(flat_data={**FIFTY, "k07": "changed"}, sync_mode=SMART, labels=LABELS, **WRITE)
    [ops] = zk.commits
    assert out == {"written": 1, "unchanged": 49}
    assert [(op, path) for op, path, _ in ops] == [("set", "/acme/prod/k07"), ("set", "/acme/prod/__vartrack__")]


def test_zookeeper_smart_repair_with_nothing_different_runs_no_transaction(zk_sink):
    zk = _zk_with(FIFTY)
    out = zk_sink(zk)._write(flat_data=FIFTY, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out == {"written": 0, "unchanged": 50} and zk.commits == []


def test_zookeeper_does_not_rewrite_a_znode_that_holds_the_value_in_any_mode(zk_sink):
    """Setting it would bump its version and fire every watch on it: so an upsert skips it too."""
    zk = _zk_with(FIFTY)
    out = zk_sink(zk)._write(flat_data=FIFTY, sync_mode=UPSERT, labels=LABELS, **WRITE)
    assert out == {"written": 0, "unchanged": 50} and zk.commits == []


def test_zookeeper_that_cannot_be_read_gets_every_znode_and_says_so(zk_sink):
    zk = _zk_with({"a": "1"})
    zk.get = MagicMock(side_effect=OSError("session expired"))
    out = zk_sink(zk)._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out["written"] == 2 and out["sync_mode"] == "GIT_UPSERT_ALL" and "session expired" in out["note"]


# ── S3 ────────────────────────────────────────────────────────────────────────

class NoSuchKey(Exception): ...


def _s3(held: dict | None, **cfg):
    from app.pipeline.sinks.s3 import S3Sink

    sink = S3Sink(settings=Settings.of("s3", bucket="b", **cfg))
    sink._s3 = MagicMock()
    sink._s3.exceptions.NoSuchKey = NoSuchKey
    if held is None:
        sink._s3.get_object.side_effect = NoSuchKey()
    else:
        sink._s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(held).encode())}
    return sink


def test_s3_leaves_an_object_that_already_holds_the_file():
    sink = _s3({"a": "1", "b": "2"})
    out = sink._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out == {"written": 0, "unchanged": 2} and not sink._s3.put_object.called


def test_s3_writes_the_whole_object_when_it_differs_or_is_missing():
    sink = _s3({"a": "1", "b": "old"})
    out = sink._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out["written"] == 2 and out["unchanged"] == 0 and sink._s3.put_object.call_count == 1
    sink = _s3(None)
    assert sink._write(flat_data={"a": "1"}, sync_mode=SMART, labels=LABELS, **WRITE)["written"] == 1


def test_s3_object_with_an_extra_key_is_not_the_file():
    sink = _s3({"a": "1", "gone": "x"})
    assert sink._write(flat_data={"a": "1"}, sync_mode=SMART, labels=LABELS, **WRITE)["written"] == 1


def test_s3_upsert_all_puts_the_object_every_time():
    sink = _s3({"a": "1"})
    assert sink._write(flat_data={"a": "1"}, sync_mode=UPSERT, labels=LABELS, **WRITE)["written"] == 1
    assert sink._s3.put_object.call_count == 1


def test_s3_that_cannot_be_read_puts_the_object_and_says_so():
    sink = _s3({"a": "1"})
    sink._s3.get_object.side_effect = OSError("endpoint unreachable")
    out = sink._write(flat_data={"a": "1"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out["written"] == 1 and out["sync_mode"] == "GIT_UPSERT_ALL" and "endpoint unreachable" in out["note"]


def test_s3_one_object_per_key_puts_only_the_keys_that_differ():
    sink = _s3({}, update_strategy="STRATEGY_KEY_VALUE")
    bodies = {"a": b"1", "b": b"old"}

    def get_object(Bucket, Key):                                   # noqa: N803  (boto3's names)
        name = Key.rsplit("/", 1)[-1]
        if name not in bodies:
            raise NoSuchKey()
        return {"Body": io.BytesIO(bodies[name])}

    sink._s3.get_object.side_effect = get_object
    out = sink._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=LABELS, **WRITE)
    assert out == {"written": 1, "unchanged": 1}
    assert sink._s3.put_object.call_args.kwargs["Key"].endswith("/b")


# ── a file on a Linux server ──────────────────────────────────────────────────

def _linux(tmp_path, **cfg):
    from app.pipeline.sinks.linux_server import LinuxServerSink

    target = tmp_path / "app.env"
    sink = LinuxServerSink(settings=Settings.of("linux_server", file_paths=[str(target)], **cfg),
                           rule_config={"env_as_filename": False})
    return sink, target


def _write_env(sink, flat, mode):
    return sink._write(flat_data=flat, sync_mode=mode, labels=MagicMock(tenant_id="acme"), **WRITE)


def test_linux_file_that_already_holds_the_keys_is_not_written_again(tmp_path):
    sink, target = _linux(tmp_path)
    assert _write_env(sink, {"a": "1", "b": "two words"}, UPSERT)["written"] == 2
    first = target.stat().st_mtime_ns
    out = _write_env(sink, {"a": "1", "b": "two words"}, SMART)
    assert out["written"] == 0 and out["unchanged"] == 2 and target.stat().st_mtime_ns == first


def test_linux_file_that_differs_is_written_whole(tmp_path):
    sink, target = _linux(tmp_path)
    _write_env(sink, {"a": "1", "b": "2"}, UPSERT)
    out = _write_env(sink, {"a": "1", "b": "3"}, SMART)
    assert out["written"] == 2 and out["unchanged"] == 0 and "b=3" in target.read_text()


def test_linux_file_edited_by_hand_is_put_back(tmp_path):
    sink, target = _linux(tmp_path)
    _write_env(sink, {"a": "1"}, UPSERT)
    target.write_text(target.read_text() + "extra=1\n")
    assert _write_env(sink, {"a": "1"}, SMART)["written"] == 1
    assert "extra" not in target.read_text()


def test_linux_with_no_file_yet_writes_it(tmp_path):
    sink, target = _linux(tmp_path)
    assert _write_env(sink, {"a": "1"}, SMART)["written"] == 1 and target.exists()


def test_linux_file_that_cannot_be_read_is_written_and_says_so(tmp_path):
    sink, target = _linux(tmp_path)
    target.write_text("# vartrack\n\x00\x01 not a file we can read\n")
    sink._fs = MagicMock(read=MagicMock(side_effect=PermissionError("denied")))
    out = _write_env(sink, {"a": "1"}, SMART)
    assert out["sync_mode"] == "GIT_UPSERT_ALL" and "denied" in out["note"]


# ── a ConfigMap ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def kubernetes_errors(monkeypatch):
    """tests/conftest.py stands a bare mock in for kubernetes; the sink imports its exception class."""
    import sys
    import types

    module = types.ModuleType("kubernetes.client.exceptions")
    module.ApiException = type("ApiException", (Exception,), {"status": 0})
    monkeypatch.setitem(sys.modules, "kubernetes.client.exceptions", module)


def _configmap(held: dict | None):
    from app.pipeline.sinks.configmap import ConfigmapSink

    sink = ConfigmapSink.__new__(ConfigmapSink)
    sink._k8s_client, sink._api = MagicMock(), MagicMock()
    sink._key_format = key_format_from_str("flat")
    sink._resolve_cm_target = lambda env: ("ns", "cm")
    sink._api.read_namespaced_config_map.return_value = SimpleNamespace(
        data=held, metadata=SimpleNamespace(resource_version="1"))
    return sink


def _labels():
    return MagicMock(as_k8s_labels=lambda: {"managed-by": "vartrack"})


def test_configmap_that_already_holds_the_keys_is_not_replaced():
    sink = _configmap({"a": "1", "b": "2"})
    out = sink._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=_labels(), **WRITE)
    assert out == {"written": 0, "unchanged": 2} and not sink._api.replace_namespaced_config_map.called


def test_configmap_that_differs_is_replaced():
    sink = _configmap({"a": "1", "b": "old"})
    out = sink._write(flat_data={"a": "1", "b": "2"}, sync_mode=SMART, labels=_labels(), **WRITE)
    assert out["written"] == 2 and sink._api.replace_namespaced_config_map.call_count == 1


def test_configmap_upsert_all_replaces_it_every_time():
    sink = _configmap({"a": "1"})
    sink._write(flat_data={"a": "1"}, sync_mode=UPSERT, labels=_labels(), **WRITE)
    assert sink._api.replace_namespaced_config_map.call_count == 1


# ── Helm ──────────────────────────────────────────────────────────────────────

def _helm(tmp_path, **cfg):
    from app.pipeline.sinks.helm import HelmSink

    return HelmSink(settings=Settings.of("helm", values_file_path=str(tmp_path / "values.yaml"), **cfg))


HELM_LABELS = MagicMock(as_helm_annotations=lambda: {"source-commit": "abc"})


def test_helm_values_file_that_already_holds_the_values_is_not_written(tmp_path, monkeypatch):
    from app.utils import atomic_file

    sink = _helm(tmp_path)
    sink._write(flat_data={"a.b": "1", "c": "2"}, sync_mode=UPSERT, labels=HELM_LABELS, **WRITE)
    spy = MagicMock()
    monkeypatch.setattr(atomic_file, "write_atomic", spy)
    out = sink._write(flat_data={"a.b": "1", "c": "2"}, sync_mode=SMART, labels=HELM_LABELS, **WRITE)
    assert out["written"] == 0 and out["unchanged"] == 2 and not spy.called


def test_helm_values_file_that_differs_is_written(tmp_path):
    sink = _helm(tmp_path)
    sink._write(flat_data={"a": "1"}, sync_mode=UPSERT, labels=HELM_LABELS, **WRITE)
    out = sink._write(flat_data={"a": "2"}, sync_mode=SMART, labels=HELM_LABELS, **WRITE)
    assert out["written"] == 1 and "a: '2'" in (tmp_path / "values.yaml").read_text() or "a: 2" in (tmp_path / "values.yaml").read_text()


def _release(sink, values):
    sink._release_values = lambda env: values
    sink._strategy = MagicMock(spec=["apply", "_reuse_values"], _reuse_values=False)
    sink._strategy.apply.return_value = {"written": 2}
    return sink


def test_helm_release_that_already_holds_the_values_is_not_upgraded_again(tmp_path):
    sink = _release(_helm(tmp_path), {"a": "1", "b": {"c": "2"}, "commonAnnotations": {"x": "stamp"}})
    out = sink._write(flat_data={"a": "1", "b.c": "2"}, sync_mode=SMART, labels=HELM_LABELS, **WRITE)
    assert out["written"] == 0 and out["unchanged"] == 2 and not sink._strategy.apply.called


def test_helm_release_with_other_values_is_upgraded_once(tmp_path):
    sink = _release(_helm(tmp_path), {"a": "1", "b": {"c": "old"}})
    out = sink._write(flat_data={"a": "1", "b.c": "2"}, sync_mode=SMART, labels=HELM_LABELS, **WRITE)
    assert out["written"] == 2 and sink._strategy.apply.call_count == 1


def test_helm_release_applied_with_reuse_values_ignores_values_other_tools_set(tmp_path):
    sink = _release(_helm(tmp_path), {"a": "1", "someone": "else"})
    sink._strategy._reuse_values = True
    out = sink._write(flat_data={"a": "1"}, sync_mode=SMART, labels=HELM_LABELS, **WRITE)
    assert out["written"] == 0 and not sink._strategy.apply.called


def test_helm_that_cannot_read_the_release_upgrades_and_says_so(tmp_path):
    sink = _release(_helm(tmp_path), {})
    sink._release_values = MagicMock(side_effect=RuntimeError("helm get values: forbidden"))
    out = sink._write(flat_data={"a": "1"}, sync_mode=SMART, labels=HELM_LABELS, **WRITE)
    assert sink._strategy.apply.call_count == 1
    assert out["sync_mode"] == "GIT_UPSERT_ALL" and "forbidden" in out["note"]


# ── what the sync says about it ───────────────────────────────────────────────

def test_the_task_says_what_was_left_alone():
    from app.ui.recorder import _written_sentence

    assert _written_sentence(3, 812) == "3 keys written, 812 unchanged"
    assert _written_sentence(0, 812) == "nothing to change (812 keys already match Git)"
    assert _written_sentence(0, 1) == "nothing to change (1 key already matches Git)"
    assert _written_sentence(1, 0) == "1 key written"
    assert _written_sentence(0, 0) == "0 keys written"


# ── LIVE_STATE is what it always was ──────────────────────────────────────────

def test_live_state_runs_as_upsert_all_and_says_so_once(caplog):
    import app.pipeline.stage_sync as stage_sync

    stage_sync._live_state_said = False
    with caplog.at_level("WARNING"):
        assert stage_sync._decide_sync_mode(10, SyncMode.LIVE_STATE) == UPSERT
        assert stage_sync._decide_sync_mode(900, SyncMode.LIVE_STATE) == UPSERT      # whatever the size
    assert caplog.text.count("LIVE_STATE is written like GIT_UPSERT_ALL") == 1


def test_the_validator_warns_about_live_state_and_still_loads_it():
    from app.config_validator import Result, _live_state

    result = Result()
    _live_state({"sync_mode": "LIVE_STATE"}, "bundle.json: the gitea/redis rule", result)
    _live_state({"sync_mode": "SYNC_MODE_LIVE_STATE"}, "x", result)
    _live_state({"sync_mode": "GIT_SMART_REPAIR"}, "y", result)
    _live_state({}, "z", result)
    assert result.errors == [] and len(result.warnings) == 2
    assert "written like GIT_UPSERT_ALL" in result.warnings[0] and "bundle.json" in result.warnings[0]
