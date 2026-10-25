"""
A sync owns its target while it writes, and prunes only what it wrote.

One sync at a time per datasource and environment, held as a lease. Prune
runs once per environment, after all its files are written, as an inventory
diff: the keys varTrack wrote before that nothing holds any more. A stale key
something else changed is left alone.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline import stage_sync
from app.pipeline.models import ETLFile, ETLResult, PayloadContext
from app.pipeline.sinks.base import UNREADABLE
from app.ui import ledger, recorder

TENANT, DS = "acme", "redis"
REPO = "http://gitea:3000/acme/configs.git"


class FakeLock:
    def __init__(self, free=True):
        self.free, self.released = free, False

    def acquire(self):
        return self.free

    def release(self):
        self.released = True


class FakeRedis:
    def __init__(self):
        self.kv, self.sets = {}, {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, *a, **kw):
        self.kv[key] = value

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def smembers(self, key):
        return set(self.sets.get(key, ()))

    def lock(self, *a, **kw):
        return FakeLock()


# ── Locks ─────────────────────────────────────────────────────────────────────

def test_locks_are_taken_in_order_and_released():
    taken = []
    redis = MagicMock()
    redis.lock.side_effect = lambda name, **kw: taken.append((name, kw)) or FakeLock()
    with patch("app.ui.ledger._redis", return_value=redis):
        with stage_sync.target_locks("acme", "redis", ["staging", "production", "staging"]):
            pass
    assert [n for n, _ in taken] == ["vt:sync-lock:acme:redis:production", "vt:sync-lock:acme:redis:staging"]
    assert taken[0][1] == {"timeout": stage_sync.LOCK_LEASE_SECONDS, "blocking_timeout": stage_sync.LOCK_WAIT_SECONDS}


def test_a_busy_target_fails_the_sync():
    redis = MagicMock()
    first = FakeLock()
    redis.lock.side_effect = [first, FakeLock(free=False)]
    with patch("app.ui.ledger._redis", return_value=redis):
        with pytest.raises(stage_sync.TargetBusy, match="redis/staging is still running"):
            with stage_sync.target_locks("acme", "redis", ["production", "staging"]):
                pass
    assert first.released


# ── A fake keyed sink and a ledger on a fake Redis ────────────────────────────

class FakeSink:
    """A sink that keeps single keys: what is in `store` is what it holds."""

    def __init__(self, store=None, *, prunes=True, fail_write_on=None, fail_delete=False, fail_read=False):
        self.store = {} if store is None else store          # env -> {key: value}
        self.prunes = prunes
        self.fail_write_on, self.fail_delete, self.fail_read = fail_write_on, fail_delete, fail_read
        self.deleted = []                                     # (env, keys)
        self.strict_reads = False

    def write(self, *, flat_data, env, file_path, **kw):
        if file_path == self.fail_write_on:
            raise RuntimeError("boom")
        self.store.setdefault(env, {}).update(flat_data)
        return {"written": len(flat_data)}

    def read_values(self, keys, datasource, env):
        if self.fail_read:
            raise ConnectionError("sink down")
        return {k: self.store.get(env, {}).get(k) for k in keys if k in self.store.get(env, {})}

    def delete_keys(self, keys, *, datasource, env):
        if self.fail_delete:
            raise ConnectionError("delete refused")
        self.deleted.append((env, sorted(keys)))
        n = 0
        for k in keys:
            n += self.store.get(env, {}).pop(k, None) is not None
        return n

    def delete_file_data(self, *a, **kw):
        return 0

    def close(self):
        pass


def _sink_class(prunes=True):
    """FakeSink with the classmethods stage_sync asks the sink's class."""
    class Sink(FakeSink):
        @classmethod
        def prunes_keys(cls, rule_config):
            return prunes

        @classmethod
        def key_space(cls, rule_config, tenant_id, env):
            return rule_config.get("space") or f"{tenant_id}/{env}"

        @classmethod
        def store(cls, rule_config):
            return rule_config.get("store", "")
    return Sink


@pytest.fixture
def redis():
    fake = FakeRedis()
    fake.rules = []  # (tenant, rule) of every tenant's rules, as stage_sync._every_rule lists them
    with patch("app.ui.ledger._redis", return_value=fake), \
         patch("app.ui.ledger.record_task"), \
         patch("app.pipeline.stage_sync._every_rule", side_effect=lambda: list(fake.rules)), \
         patch.object(ledger, "record_event") as event:
        fake.events = event
        yield fake


def _file(path, env, **flat):
    return ETLFile(file_path=path, env=env, flat_data=dict(flat), root_key="")


def _sync(sink, files, *, repo=REPO, prune=True, rule_extra=None, ds=DS):
    """One webhook sync of *files* to datasource *ds*: stage_sync, then the recorder, as a task does."""
    rule = {"datasource": ds, "prune": prune, **(rule_extra or {})}
    ctx = PayloadContext(platform="github", repo_url=repo, ref="main", branch="main", commit_sha="abc",
                         tag=None, pr_number=None, rule_config=rule, parsed_payload={})
    etl = ETLResult()
    etl.files.extend(files)
    with patch("app.pipeline.sinks.create_sink", return_value=sink):
        results = stage_sync.run_stage_sync(ctx, etl, ds, TENANT, len(files))
    task = {"taskId": "t1", "rule": f"github/{ds}", "datasource": ds, "trigger": "webhook", "createdAt": "x",
            "finishedAt": "y", "message": "", "ref": "main", "revision": "abc", "_platform": "github",
            "_repoUrl": repo, "dryRun": False}
    recorder.task_finished(task, tenant=TENANT, etl=etl, results=results)
    return results


def _target(env):
    return ledger.get_target(TENANT, DS, env)


# ── The inventory diff ────────────────────────────────────────────────────────

def test_a_push_touching_one_file_never_prunes_the_environments_other_files(redis):
    cls = _sink_class()
    sink = cls()
    _sync(sink, [_file("a.yaml", "production", **{"db.host": "x"}), _file("b.yaml", "production", **{"cache.ttl": "5"})])
    sink.deleted.clear()

    _sync(sink, [_file("a.yaml", "production", **{"db.host": "y"})])  # only a.yaml was pushed

    assert sink.deleted == []
    assert sink.store["production"] == {"db.host": "y", "cache.ttl": "5"}


def test_a_key_removed_from_the_pushed_file_is_pruned(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"db.host": "x", "db.old": "1"})])

    results = _sync(sink, [_file("a.yaml", "production", **{"db.host": "x"})])

    assert sink.deleted == [("production", ["db.old"])]
    assert results[-1].pruned == 1 and results[-1].prune_pending == []
    assert "db.old" not in sink.store["production"]
    assert "db.old" not in _target("production")["desired"]


def test_two_repositories_feeding_one_environment_do_not_prune_each_other(redis):
    sink = _sink_class()()
    _sync(sink, [_file("app.yaml", "production", **{"a": "1"})], repo="http://gitea:3000/acme/one.git")
    _sync(sink, [_file("app.yaml", "production", **{"b": "2"})], repo="http://gitea:3000/acme/two.git")

    assert sink.deleted == []
    assert set(_target("production")["desiredByFile"]) == {"acme/one:app.yaml", "acme/two:app.yaml"}
    assert sink.store["production"] == {"a": "1", "b": "2"}


def test_environments_sharing_a_key_space_keep_each_others_keys(redis):
    cls = _sink_class()
    sink = cls()
    shared = {"space": "preview"}  # staging and develop land on the same target
    _sync(sink, [_file("a.yaml", "staging", **{"X": "1"})], rule_extra=shared)
    _sync(sink, [_file("a.yaml", "develop", **{"X": "1", "Y": "2"})], rule_extra=shared)

    # staging drops X, but develop still writes it to the same place
    _sync(sink, [_file("a.yaml", "staging", **{"Z": "3"})], rule_extra=shared)

    assert sink.deleted == []


def test_environments_with_their_own_key_space_prune_independently(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "staging", **{"X": "1"})])
    _sync(sink, [_file("a.yaml", "develop", **{"X": "1"})])

    _sync(sink, [_file("a.yaml", "staging", **{"Z": "3"})])

    assert sink.deleted == [("staging", ["X"])]


def _two_datasources_on(redis, *, store_a="r1", store_b="r1", space_a="shared", space_b="shared"):
    """redis (DS) and redis-dr, each with its own settings, both listed as rules."""
    a = {"space": space_a, "store": store_a}
    b = {"space": space_b, "store": store_b}
    redis.rules = [(TENANT, {"datasource": DS, **a}), (TENANT, {"datasource": "redis-dr", **b})]
    return a, b


def test_datasources_writing_the_same_store_and_key_space_keep_each_others_keys(redis):
    cls = _sink_class()
    sink = cls()
    a, b = _two_datasources_on(redis)
    with patch("app.pipeline.sinks.sink_class", return_value=cls):
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=a)
        _sync(sink, [_file("a.yaml", "production", **{"X": "1", "Y": "2"})], rule_extra=b, ds="redis-dr")

        # redis drops X, but redis-dr still writes it to the same place
        _sync(sink, [_file("a.yaml", "production", **{"Z": "3"})], rule_extra=a)

    assert sink.deleted == []


def test_datasources_on_different_stores_prune_independently(redis):
    cls = _sink_class()
    sink = cls()
    a, b = _two_datasources_on(redis, store_b="r2")  # same key space, another server
    with patch("app.pipeline.sinks.sink_class", return_value=cls):
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=a)
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=b, ds="redis-dr")

        _sync(sink, [_file("a.yaml", "production", **{"Z": "3"})], rule_extra=a)

    assert sink.deleted == [("production", ["X"])]


def test_datasources_on_one_store_with_their_own_key_spaces_prune_independently(redis):
    cls = _sink_class()
    sink = cls()
    a, b = _two_datasources_on(redis, space_a="hash-a", space_b="hash-b")
    with patch("app.pipeline.sinks.sink_class", return_value=cls):
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=a)
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=b, ds="redis-dr")

        _sync(sink, [_file("a.yaml", "production", **{"Z": "3"})], rule_extra=a)

    assert sink.deleted == [("production", ["X"])]


def test_a_datasource_of_another_sink_type_is_not_compared(redis):
    cls = _sink_class()
    sink = cls()
    a, b = _two_datasources_on(redis)
    with patch("app.pipeline.sinks.sink_class", side_effect=lambda ds: cls if ds == DS else object):
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=a)
        _sync(sink, [_file("a.yaml", "production", **{"X": "1"})], rule_extra=b, ds="redis-dr")

        _sync(sink, [_file("a.yaml", "production", **{"Z": "3"})], rule_extra=a)

    assert sink.deleted == [("production", ["X"])]


def test_when_the_rules_cannot_be_listed_nothing_is_pruned(redis):
    cls = _sink_class()
    sink = cls()
    _sync(sink, [_file("a.yaml", "production", **{"X": "1"})])
    with patch("app.pipeline.stage_sync._every_rule", side_effect=RuntimeError("schema repo down")):
        results = _sync(sink, [_file("a.yaml", "production", **{"Z": "3"})])

    assert sink.deleted == []
    assert results[-1].pruned == 0


def test_the_first_sync_of_an_environment_prunes_nothing(redis):
    sink = _sink_class()({"production": {"by-hand": "1"}})
    _sync(sink, [_file("a.yaml", "production", a="1")])
    assert sink.deleted == []
    assert sink.store["production"]["by-hand"] == "1"


def test_keys_nobody_wrote_are_never_pruned(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", b="2")])
    sink.store["production"]["added-by-hand"] = "9"

    _sync(sink, [_file("a.yaml", "production", a="1")])

    assert sink.store["production"] == {"a": "1", "added-by-hand": "9"}


# ── What stops a prune ────────────────────────────────────────────────────────

def test_protected_keys_match_the_flat_key_and_are_kept(redis):
    sink = _sink_class()()
    rule = {"prune": {"enabled": True, "protection": ["legacy.*"]}}
    _sync(sink, [_file("a.yaml", "production", **{"a": "1", "legacy.flag": "1", "old": "1"})], prune=rule["prune"])

    _sync(sink, [_file("a.yaml", "production", a="1")], prune=rule["prune"])

    assert sink.deleted == [("production", ["old"])]
    assert "legacy.flag" in sink.store["production"]


def test_a_stale_key_changed_outside_varTrack_is_kept_and_reported(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", changed="orig", same="s")])
    sink.store["production"]["changed"] = "someone edited this"

    results = _sync(sink, [_file("a.yaml", "production", a="1")])

    assert sink.deleted == [("production", ["same"])]
    assert results[-1].prune_skipped == ["changed"]
    assert sink.store["production"]["changed"] == "someone edited this"
    assert any(c.kwargs["reason"] == "PruneSkipped" for c in redis.events.call_args_list)


def test_a_secret_is_compared_by_digest(redis):
    sink = _sink_class()()
    sec = ETLFile(file_path="a.yaml", env="production", flat_data={"a": "1", "token": "s3cret"}, root_key="",
                  secret_fields=frozenset({"token"}))
    _sync(sink, [sec])
    sink.store["production"]["token"] = "rotated by someone"
    _sync(sink, [_file("a.yaml", "production", a="1")])
    assert sink.deleted == [] and sink.store["production"]["token"] == "rotated by someone"

    sink2 = _sink_class()()
    ledger_redis = FakeRedis()
    with patch("app.ui.ledger._redis", return_value=ledger_redis):
        _sync(sink2, [sec])
        _sync(sink2, [_file("a.yaml", "production", a="1")])
    assert sink2.deleted == [("production", ["token"])]  # untouched since varTrack wrote it


def test_an_unreadable_stale_key_is_deleted_and_a_gone_one_leaves_the_inventory(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", write_only="1", gone="1")])
    sink.store["production"].pop("gone")
    original = sink.read_values
    sink.read_values = lambda keys, ds, env: {**original(keys, ds, env),
                                              **({"write_only": UNREADABLE} if "write_only" in keys else {})}

    _sync(sink, [_file("a.yaml", "production", a="1")])

    assert sink.deleted == [("production", ["write_only"])]
    assert _target("production")["pendingPrune"] == {}


def test_prune_dry_run_deletes_nothing_and_keeps_the_keys_pending(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")], prune={"enabled": True, "dry_run": True})

    results = _sync(sink, [_file("a.yaml", "production", a="1")], prune={"enabled": True, "dry_run": True})

    assert sink.deleted == []
    assert results[-1].prune_would_delete == ["old"]
    assert list(_target("production")["pendingPrune"]) == ["old"]
    # dry run turned off: the pending key is deleted by the next sync
    _sync(sink, [_file("a.yaml", "production", a="1")])
    assert sink.deleted == [("production", ["old"])]


def test_nothing_is_pruned_when_prune_is_off(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")], prune=False)
    _sync(sink, [_file("a.yaml", "production", a="1")], prune=False)
    assert sink.deleted == []


def test_nothing_is_pruned_when_a_file_failed(redis):
    sink = _sink_class()(fail_write_on="b.yaml")
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])

    results = _sync(sink, [_file("a.yaml", "production", a="1"), _file("b.yaml", "production", b="2")])

    assert [r.status for r in results] == ["ok", "error"]
    assert sink.deleted == []


def test_nothing_is_pruned_when_the_ledger_cannot_be_read(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])
    with patch("app.ui.ledger.get_target", side_effect=ConnectionError("down")):
        ctx = PayloadContext("github", REPO, "main", "main", "abc", None, None, {"datasource": DS, "prune": True}, {})
        etl = ETLResult()
        etl.files.append(_file("a.yaml", "production", a="1"))
        with patch("app.pipeline.sinks.create_sink", return_value=sink):
            stage_sync.run_stage_sync(ctx, etl, DS, TENANT, 1)
    assert sink.deleted == []


def test_nothing_is_pruned_when_the_sink_cannot_be_read_and_the_keys_stay_owned(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])
    sink.fail_read = True

    _sync(sink, [_file("a.yaml", "production", a="1")])
    assert sink.deleted == [] and list(_target("production")["pendingPrune"]) == ["old"]

    sink.fail_read = False
    _sync(sink, [_file("a.yaml", "production", a="1")])  # the next sync retries
    assert sink.deleted == [("production", ["old"])]


def test_a_failed_delete_keeps_the_writes_and_retries_next_sync(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])
    sink.fail_delete = True

    results = _sync(sink, [_file("a.yaml", "production", a="2")])

    assert results[-1].status == "ok" and results[-1].prune_error == "delete refused"
    assert sink.store["production"]["a"] == "2"            # the write stands
    assert list(_target("production")["pendingPrune"]) == ["old"]
    assert any(c.kwargs["reason"] == "PruneFailed" for c in redis.events.call_args_list)

    sink.fail_delete = False
    _sync(sink, [_file("a.yaml", "production", a="2")])
    assert sink.deleted == [("production", ["old"])]
    assert _target("production")["pendingPrune"] == {}


def test_a_pending_key_written_again_is_no_longer_stale(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])
    sink.fail_delete = True
    _sync(sink, [_file("a.yaml", "production", a="1")])
    sink.fail_delete = False

    _sync(sink, [_file("a.yaml", "production", a="1", old="back")])

    assert sink.deleted == [] and _target("production")["pendingPrune"] == {}


def test_a_sink_that_replaces_a_whole_object_is_never_asked_to_delete_keys(redis):
    sink = _sink_class(prunes=False)()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])
    results = _sync(sink, [_file("a.yaml", "production", a="1")])
    assert sink.deleted == [] and results[-1].prune_pending is None


# ── The ledger ────────────────────────────────────────────────────────────────

def test_the_ledger_keys_each_file_by_repository_and_drops_the_old_bare_key(redis):
    redis.kv[ledger._key("target", TENANT, DS, "production")] = ledger.json.dumps({
        "desiredByFile": {"a.yaml": {"desired": {"old": "1"}, "secretDigests": {}}}})
    ledger.record_target(tenant=TENANT, platform="github", datasource=DS, env="production", repo_url=REPO,
                         ref="main", revision="abc", file_path="a.yaml", flat_data={"new": "2"},
                         secret_fields=[], commit_message="", author="", sync={})

    target = _target("production")
    assert list(target["desiredByFile"]) == ["acme/configs:a.yaml"]
    assert target["files"] == ["a.yaml"] and target["desired"] == {"new": "2"}


def test_key_state():
    assert ledger.key_state("1", "1") == "synced"
    assert ledger.key_state("1", "2") == "drifted"
    assert ledger.key_state("1", None) == "missing"
    assert ledger.key_state("***", UNREADABLE, ledger.secret_digest("x")) == "unreadable"
    assert ledger.key_state("***", "x", ledger.secret_digest("x")) == "synced"
    assert ledger.key_state("***", "y", ledger.secret_digest("x")) == "drifted"


# ── delete_keys, per sink ─────────────────────────────────────────────────────

def _redis_sink(structure):
    from app.pipeline.sinks.key_formatter import KeyFormat
    from app.pipeline.sinks.redis import RedisSink

    sink = RedisSink.__new__(RedisSink)
    sink._structure, sink._hash_key = structure, "vartrack"
    sink._key_format = KeyFormat.DOT if structure == "HASH" else KeyFormat.FLAT
    sink._dest_tpl, sink._prefix, sink._tenant_id = "", "", "acme"
    sink._redis = MagicMock()
    return sink


def test_redis_hash_deletes_only_the_given_fields():
    sink = _redis_sink("HASH")
    sink._redis.hdel.return_value = 2
    assert sink.delete_keys({"a.b", "c"}, datasource="redis", env="production") == 2
    args = sink._redis.hdel.call_args.args
    assert args[0] == "acme:redis:production:vartrack" and sorted(args[1:]) == ["a.b", "c"]


def test_redis_strings_deletes_only_the_given_keys():
    sink = _redis_sink("STRING")
    sink._redis.delete.return_value = 1
    assert sink.delete_keys({"a.b"}, datasource="redis", env="production") == 1
    sink._redis.delete.assert_called_once_with("acme:redis:production:a_b")


def test_redis_json_structure_has_nothing_to_prune_and_key_space_follows_the_prefix():
    from app.pipeline.sinks.redis import RedisSink

    assert RedisSink.prunes_keys({"redis_data_structure": "json"}) is False
    assert RedisSink.prunes_keys({}) is True
    a = RedisSink.key_space({"datasource": "redis", "redis_key_prefix": "fixed"}, "acme", "staging")
    b = RedisSink.key_space({"datasource": "redis", "redis_key_prefix": "fixed"}, "acme", "develop")
    c = RedisSink.key_space({"datasource": "redis"}, "acme", "staging")
    d = RedisSink.key_space({"datasource": "redis"}, "acme", "develop")
    assert a == b and c != d


def test_vercel_deletes_a_variable_only_when_this_environment_owns_all_its_targets():
    from app.pipeline.sinks.vercel import VercelSink

    sink = VercelSink.__new__(VercelSink)
    sink._http = MagicMock()
    sink._api_base, sink._project_id, sink._team_id, sink._targets = "https://api", "prj", None, None
    sink._fetch_existing = lambda: {
        "OLD": {"id": "1", "target": ["preview"]},
        "SHARED": {"id": "2", "target": ["preview", "production"]},
    }
    deleted = []
    sink._delete_env_var = deleted.append

    assert sink.delete_keys({"OLD", "SHARED", "GONE"}, datasource="vercel", env="staging") == 1
    assert deleted == ["1"]


def test_vercel_environments_other_than_production_share_a_key_space():
    from app.pipeline.sinks.vercel import VercelSink

    rule = {"vercel_project_id": "prj"}
    assert VercelSink.key_space(rule, "acme", "staging") == VercelSink.key_space(rule, "acme", "develop")
    assert VercelSink.key_space(rule, "acme", "staging") != VercelSink.key_space(rule, "acme", "production")


def test_zookeeper_deletes_exactly_the_given_leaf_znodes():
    from app.pipeline.sinks.key_formatter import KeyFormat
    from app.pipeline.sinks.zookeeper import ZookeeperSink

    sink = ZookeeperSink.__new__(ZookeeperSink)
    sink._key_format = KeyFormat.SLASH
    sink._base_path, sink._tenant_id, sink._path_template = "/vt", "acme", "/{env}"
    zk = MagicMock()
    zk.exists.return_value = True
    zk.get_children.side_effect = lambda path: ["child"] if path.endswith("/db") else []
    sink._zk = zk

    assert sink.delete_keys({"db", "db.old"}, datasource="zk", env="production") == 1
    zk.delete.assert_called_once_with("/vt/production/db/old")  # /db has children: kept
    zk.delete.assert_called_once()


def test_zookeeper_key_space_is_the_root_path():
    from app.pipeline.sinks.zookeeper import ZookeeperSink

    rule = {"zk_base_path": "/vt", "destination_template": "/{env}"}
    assert ZookeeperSink.key_space(rule, "acme", "staging") == "/vt/staging"
    assert ZookeeperSink.key_space({"zk_base_path": "/vt", "destination_template": "/shared"}, "acme", "a") == \
        ZookeeperSink.key_space({"zk_base_path": "/vt", "destination_template": "/shared"}, "acme", "b")


def test_mongo_deletes_documents_by_key_inside_the_environment():
    from app.pipeline.sinks.mongo import MongoSink

    sink = MongoSink.__new__(MongoSink)
    sink._tenant_id = "acme"
    collection = MagicMock()
    collection.bulk_write.return_value = SimpleNamespace(deleted_count=2)
    sink._collection_for_env = lambda env: collection
    operations = []
    with patch.dict("sys.modules", {"pymongo": SimpleNamespace(DeleteOne=lambda flt: operations.append(flt) or flt)}):
        assert sink.delete_keys({"a", "b"}, datasource="mongo", env="production") == 2
    assert sorted(op["key"] for op in operations) == ["a", "b"]
    assert all(op["_vt_env"] == "production" and op["_vt_tenant"] == "acme" for op in operations)


def test_mongo_only_prunes_the_key_value_strategy_and_only_when_it_is_asked_for():
    from app.pipeline.sinks.mongo import MongoSink

    # Unset is STRATEGY_DOCUMENT: it replaces the file's document on every write, so no key is left to prune.
    assert MongoSink.prunes_keys({}) is False
    assert MongoSink.prunes_keys({"update_strategy": "STRATEGY_UNSPECIFIED"}) is False
    assert MongoSink.prunes_keys({"update_strategy": "STRATEGY_KEY_VALUE"}) is True
    assert MongoSink.prunes_keys({"update_strategy": "STRATEGY_FILE"}) is False
    assert MongoSink.prunes_keys({"update_strategy": "STRATEGY_DOCUMENT"}) is False
    assert MongoSink.prunes_keys({"update_strategy": "nonsense"}) is False


def test_whole_object_sinks_do_not_prune():
    from app.pipeline.sinks.configmap import ConfigmapSink
    from app.pipeline.sinks.helm import HelmSink
    from app.pipeline.sinks.linux_server import LinuxServerSink
    from app.pipeline.sinks.s3 import S3Sink

    for cls in (ConfigmapSink, HelmSink, LinuxServerSink, S3Sink):
        assert cls.prunes_keys({}) is False


# ── The dry run ───────────────────────────────────────────────────────────────

def _candidates(files, *, prune=True, prunes=True):
    etl = ETLResult()
    etl.files.extend(files)
    rule = {"datasource": DS, "prune": prune}
    with patch("app.pipeline.sinks.sink_class", return_value=_sink_class(prunes)), \
         patch("app.pipeline.datasource_settings._bundle", return_value={}):
        return stage_sync.prune_candidates(etl, DS, TENANT, rule, REPO)


def test_a_dry_run_lists_the_keys_a_sync_would_see_as_stale(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1"), _file("a.yaml", "staging", a="1", gone="1")])

    out = _candidates([_file("a.yaml", "production", a="1"), _file("a.yaml", "staging", a="1", gone="1")])

    assert out == {"production": ["old"]}   # staging drops nothing
    assert sink.deleted == []


def test_a_dry_run_leaves_protected_keys_out_and_lists_nothing_when_prune_is_off(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", a="1", old="1")])

    assert _candidates([_file("a.yaml", "production", a="1")], prune={"enabled": True, "protection": ["old"]}) == {}
    assert _candidates([_file("a.yaml", "production", a="1")], prune=False) == {}
    assert _candidates([_file("a.yaml", "production", a="1")], prunes=False) == {}


# ── Which store a datasource writes to ───────────────────────────────────────

def test_a_redis_store_is_its_servers_and_database_however_they_are_written():
    from app.pipeline.sinks.redis import RedisSink

    a = RedisSink.store({"redis_hosts": ["Cache-2:6379", "cache-1:6379"], "redis_db": 0})
    b = RedisSink.store({"redis_hosts": ["cache-1:6379", "cache-2:6379"], "redis_db": 0})
    assert a == b
    assert a != RedisSink.store({"redis_hosts": ["cache-1:6379", "cache-2:6379"], "redis_db": 1})
    assert a != RedisSink.store({"redis_hosts": ["cache-1:6379"], "redis_db": 0})


def test_a_zookeeper_store_is_its_servers_and_a_vercel_store_its_team():
    from app.pipeline.sinks.vercel import VercelSink
    from app.pipeline.sinks.zookeeper import ZookeeperSink

    assert ZookeeperSink.store({"zk_hosts": ["zk1:2181"]}) == ZookeeperSink.store({"zk_hosts": ["ZK1:2181"]})
    assert ZookeeperSink.store({"zk_hosts": ["zk1:2181"]}) != ZookeeperSink.store({"zk_hosts": ["zk2:2181"]})
    assert VercelSink.store({"vercel_team_id": "team_a"}) != VercelSink.store({"vercel_team_id": "team_b"})


def test_datasources_do_not_share_mongo_documents_so_mongo_names_no_store():
    from app.pipeline.sinks.mongo import MongoSink

    # Documents are filtered by tenant, datasource and env: the key space already says it all.
    assert MongoSink.store({"mongo_uri": "mongodb://a"}) == ""
    assert MongoSink.key_space({"datasource": "mongo-a"}, "acme", "staging") !=         MongoSink.key_space({"datasource": "mongo-b"}, "acme", "staging")


# ── A change of update_strategy is reported ───────────────────────────────────

class ShapeShiftingSink(_sink_class(prunes=False)):
    """A Mongo-like sink that removes `replaced` documents of the old shape on each write."""

    replaced = 19

    def write(self, **kw):
        return {**super().write(**kw), "replaced": self.replaced, "strategy": "STRATEGY_DOCUMENT"}


def test_replaced_documents_reach_the_result_and_become_a_shape_changed_event(redis):
    results = _sync(ShapeShiftingSink(), [_file("app.yaml", "production", a="1")], prune=False)

    assert (results[0].replaced, results[0].replaced_by) == (19, "STRATEGY_DOCUMENT")
    events = [c.kwargs for c in redis.events.call_args_list if c.kwargs["reason"] == "ShapeChanged"]
    assert len(events) == 1 and events[0]["env"] == "production"
    assert events[0]["message"] == ("replaced 19 per-key documents of app.yaml with one document "
                                    "(update_strategy STRATEGY_DOCUMENT)")


def test_a_sync_that_replaced_nothing_records_no_shape_event(redis):
    _sync(_sink_class(prunes=False)(), [_file("app.yaml", "production", a="1")], prune=False)
    assert not [c for c in redis.events.call_args_list if c.kwargs["reason"] == "ShapeChanged"]


def test_the_key_value_direction_has_its_own_wording():
    from app.ui.recorder import _shape_changed
    msg = _shape_changed({"file": "app.yaml", "replaced": 1, "replacedBy": "STRATEGY_KEY_VALUE"})
    assert msg == "replaced the document of app.yaml with one document per key (update_strategy STRATEGY_KEY_VALUE)"
