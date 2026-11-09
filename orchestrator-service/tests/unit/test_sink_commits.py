"""One write, one step in the store: a Redis MULTI/EXEC, a ZooKeeper transaction, a Mongo session transaction.

The stores are fakes that apply a step whole or not at all, and refuse or die on demand, so each test says what a
failed write leaves behind."""
from __future__ import annotations

import copy
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.pipeline.sinks.key_formatter import key_format_from_str
from app.utils.enums.sync_mode import SyncMode

WRITE = dict(sync_mode=SyncMode.GIT_PUSH if hasattr(SyncMode, "GIT_PUSH") else list(SyncMode)[0],
             datasource="ds", env="prod", repo="r", branch="main", commit_sha="abc", file_path="app.yaml",
             total_sources=1)


# ── Redis ────────────────────────────────────────────────────────────────────

class FakeRedis:
    """A store that applies a pipeline when it is executed, and not before."""

    def __init__(self, *, refuse=(), die=False):
        self.data, self.pipelines, self.refuse, self.die = {}, [], set(refuse), die

    def pipeline(self, transaction=True):
        pipe = FakePipeline(self, transaction)
        self.pipelines.append(pipe)
        return pipe

    def hset(self, key, mapping=None):          # a direct call: applied at once
        self.data.setdefault(key, {}).update(mapping or {})


class FakePipeline:
    def __init__(self, store, transaction):
        self.store, self.transaction, self.queued, self.executed = store, transaction, [], 0

    def hset(self, key, mapping=None):
        self.queued.append(("hset", key, dict(mapping or {})))

    def set(self, key, value, ex=None):
        self.queued.append(("set", key, value, ex))

    def expire(self, key, seconds):
        self.queued.append(("expire", key, seconds))

    def execute(self, raise_on_error=True):
        self.executed += 1
        if self.store.die:
            raise ConnectionError("connection lost before EXEC")       # nothing of it was applied
        replies = []
        for command in self.queued:
            if command[1] in self.store.refuse:
                replies.append(RuntimeError(f"WRONGTYPE {command[1]}"))
                continue
            if command[0] == "hset":
                self.store.data.setdefault(command[1], {}).update(command[2])
            elif command[0] == "set":
                self.store.data[command[1]] = command[2]
            replies.append(1)
        return replies


def redis_sink(store, *, structure="HASH", ttl=0, clustered=False):
    from app.pipeline.sinks.redis import RedisSink

    sink = RedisSink.__new__(RedisSink)
    sink._redis, sink._structure, sink._hash_key, sink._ttl = store, structure, "vartrack", ttl
    sink._clustered, sink._key_format = clustered, key_format_from_str("dot" if structure == "HASH" else "flat")
    sink._key_prefix = lambda datasource, env: f"acme:{datasource}:{env}"
    return sink


LABELS = MagicMock(as_redis_meta=lambda: {"managed-by": "vartrack"}, as_zk_meta=lambda: b"stamp",
                   as_mongo_meta=lambda: {"_vt_managed_by": "vartrack"})


def test_redis_data_and_stamp_are_one_multi_exec():
    store = FakeRedis()
    out = redis_sink(store)._write(flat_data={"db.host": "x", "db.port": "5"}, labels=LABELS, **WRITE)
    [pipe] = store.pipelines
    assert out == {"written": 2, "unchanged": 0} and pipe.transaction and pipe.executed == 1
    assert [c[1] for c in pipe.queued] == ["acme:ds:prod:vartrack", "acme:ds:prod:__vartrack__"]
    assert store.data["acme:ds:prod:__vartrack__"] == {"managed-by": "vartrack"}


def test_redis_strings_and_their_expiry_share_the_step():
    store = FakeRedis()
    redis_sink(store, structure="STRING", ttl=60)._write(flat_data={"a": "1", "b": "2"}, labels=LABELS, **WRITE)
    [pipe] = store.pipelines
    assert [c[0] for c in pipe.queued] == ["set", "expire", "set", "expire", "hset"]
    assert pipe.executed == 1


def test_a_connection_lost_before_exec_leaves_neither_the_data_nor_the_stamp():
    store = FakeRedis(die=True)
    with pytest.raises(ConnectionError):
        redis_sink(store)._write(flat_data={"db.host": "x"}, labels=LABELS, **WRITE)
    assert store.data == {}             # the stamp used to be written on its own, after the data


def test_a_command_redis_refuses_fails_the_write():
    store = FakeRedis(refuse={"acme:ds:prod:vartrack"})
    with pytest.raises(RuntimeError, match=r"refused 1 of 2 commands.*WRONGTYPE"):
        redis_sink(store)._write(flat_data={"db.host": "x"}, labels=LABELS, **WRITE)


def test_a_redis_cluster_runs_a_pipeline_not_a_multi():
    store = FakeRedis()
    redis_sink(store, clustered=True)._write(flat_data={"db.host": "x"}, labels=LABELS, **WRITE)
    assert store.pipelines[0].transaction is False and store.pipelines[0].executed == 1


# ── ZooKeeper ────────────────────────────────────────────────────────────────

class NoNodeError(Exception): ...
class NodeExistsError(Exception): ...
class RolledBackError(Exception): ...
class NoAuthError(Exception): ...


@pytest.fixture(autouse=True)
def kazoo_errors(monkeypatch):
    """tests/conftest.py stands a mock in for kazoo.exceptions; the sink needs classes it can isinstance()."""
    import types

    module = types.ModuleType("kazoo.exceptions")
    for cls in (NoNodeError, NodeExistsError, RolledBackError, NoAuthError):
        setattr(module, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, "kazoo.exceptions", module)


class FakeTxn:
    def __init__(self, zk):
        self.zk, self.ops = zk, []

    def create(self, path, value):
        self.ops.append(("create", path, value))

    def set_data(self, path, value):
        self.ops.append(("set", path, value))

    def commit(self):
        self.zk.commits.append(self.ops)
        if self.zk.appears:                                 # a node another writer creates before the commit
            self.zk.nodes[self.zk.appears], self.zk.appears = b"other", None
        staged = dict(self.zk.nodes)
        results = []
        for op, path, value in self.ops:
            if op == "create" and path in staged:
                results.append(NodeExistsError())
            elif op == "set" and path not in staged:
                results.append(NoNodeError())
            else:
                staged[path] = value
                results.append(path)
        if any(isinstance(r, Exception) for r in results):    # one failed: all are rolled back
            return [r if isinstance(r, Exception) else RolledBackError() for r in results]
        self.zk.nodes = staged
        return results


class FakeZk:
    def __init__(self, nodes=None):
        self.nodes, self.commits, self.ensured, self.appears = dict(nodes or {}), [], [], None

    def ensure_path(self, path):
        self.ensured.append(path)
        self.nodes.setdefault(path, b"")

    def exists(self, path):
        return path in self.nodes

    def transaction(self):
        return FakeTxn(self)


@pytest.fixture
def zk_sink(monkeypatch):
    monkeypatch.setitem(sys.modules, "vartrack_core", None)   # no native differ: every key is written
    from app.pipeline.sinks.zookeeper import ZookeeperSink

    def make(zk):
        sink = ZookeeperSink.__new__(ZookeeperSink)
        sink._zk, sink._base_path, sink._tenant_id = zk, "", "acme"
        sink._path_template, sink._key_format = "/{tenant}/{env}", key_format_from_str("slash")
        return sink
    return make


def test_zookeeper_values_and_stamp_are_one_transaction(zk_sink):
    zk = FakeZk()
    out = zk_sink(zk)._write(flat_data={"db.host": "x", "db.port": "5"}, labels=LABELS, **WRITE)
    [ops] = zk.commits
    assert out == {"written": 2, "unchanged": 0}
    assert [(op, path) for op, path, _ in ops] == [
        ("create", "/acme/prod/db/host"), ("create", "/acme/prod/db/port"), ("set", "/acme/prod/__vartrack__")]
    assert zk.nodes["/acme/prod/db/host"] == b"x" and zk.nodes["/acme/prod/__vartrack__"] == b"stamp"


def test_zookeeper_updates_what_exists_and_creates_what_does_not(zk_sink):
    zk = FakeZk({"/acme/prod/db/host": b"old"})
    zk_sink(zk)._write(flat_data={"db.host": "new", "db.port": "5"}, labels=LABELS, **WRITE)
    assert [op for op, _, _ in zk.commits[0]] == ["set", "create", "set"]
    assert zk.nodes["/acme/prod/db/host"] == b"new"


def test_a_refused_transaction_leaves_every_znode_as_it_was(zk_sink, monkeypatch):
    zk = FakeZk({"/acme/prod/db/host": b"old"})
    sink = zk_sink(zk)
    monkeypatch.setattr(FakeTxn, "commit", lambda self: [RolledBackError(), NoAuthError(), RolledBackError()])
    with pytest.raises(RuntimeError, match=r"refused the transaction of 3 znodes \(NoAuthError.*none of them was written"):
        sink._write(flat_data={"db.host": "new", "db.port": "5"}, labels=LABELS, **WRITE)
    assert zk.nodes["/acme/prod/db/host"] == b"old" and "/acme/prod/__vartrack__" in zk.nodes
    assert zk.nodes["/acme/prod/__vartrack__"] == b""          # the stamp was not written either


def test_a_node_that_appears_between_looking_and_committing_is_retried_once(zk_sink):
    zk = FakeZk()
    zk.appears = "/acme/prod/db/host"
    zk_sink(zk)._write(flat_data={"db.host": "x"}, labels=LABELS, **WRITE)
    assert len(zk.commits) == 2                                 # the first failed whole, the second set the node
    assert [op for op, _, _ in zk.commits[0]] == ["create", "set"]
    assert [op for op, _, _ in zk.commits[1]] == ["set", "set"]
    assert zk.nodes["/acme/prod/db/host"] == b"x"


def test_a_file_over_the_transaction_limit_is_several_transactions_with_a_warning(zk_sink, monkeypatch, caplog):
    from app.pipeline.sinks import zookeeper

    monkeypatch.setattr(zookeeper, "_TXN_BUDGET", 400)
    zk = FakeZk()
    flat = {f"k{i}": "v" * 100 for i in range(6)}
    with caplog.at_level("WARNING"):
        zk_sink(zk)._write(flat_data=flat, labels=LABELS, **WRITE)
    assert len(zk.commits) > 1 and "transactions" in caplog.text
    for ops in zk.commits[:-1]:
        assert all(path != "/acme/prod/__vartrack__" for _, path, _ in ops)    # the stamp waits for the last
    assert zk.commits[-1][-1][1] == "/acme/prod/__vartrack__"
    assert all(zk.nodes[f"/acme/prod/k{i}"] == b"v" * 100 for i in range(6))


def test_batches_always_end_with_the_stamp():
    from app.pipeline.sinks.zookeeper import _batches

    assert _batches({}, "/m", b"s") == [{"/m": b"s"}]


# ── Mongo ────────────────────────────────────────────────────────────────────

class FakeMongo:
    """A collection of documents and a client whose transactions apply their operations whole or not at all."""

    def __init__(self, docs=None, *, replica_set=True, fail_on=None):
        self.docs, self.replica_set, self.fail_on = list(docs or []), replica_set, fail_on
        self.sessions, self.ops, self.asked = [], [], []

    # the collection
    name = "configs"

    def replace_one(self, flt, doc, upsert=False, session=None):
        self._op("replace_one", session)
        self.docs = [d for d in self.docs if not self._matches(d, flt)] + [doc]

    def delete_many(self, flt, session=None):
        self._op("delete_many", session)
        kept = [d for d in self.docs if not self._matches(d, flt)]
        result, self.docs = SimpleNamespace(deleted_count=len(self.docs) - len(kept)), kept
        return result

    def find_one(self, flt, proj=None, session=None):
        return next((d for d in self.docs if self._matches(d, flt)), None)

    @staticmethod
    def _matches(doc, flt):
        for key, want in flt.items():
            if isinstance(want, dict) and "$exists" in want:
                if (key in doc) != want["$exists"]:
                    return False
            elif doc.get(key) != want:
                return False
        return True

    def _op(self, name, session):
        self.ops.append((name, session))
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")

    # the client
    @property
    def admin(self):
        def command(name):
            self.asked.append(name)
            return {"setName": "rs0"} if self.replica_set else {"ismaster": True}
        return SimpleNamespace(command=command)

    def start_session(self):
        outer = self

        class Session:
            def __enter__(self_):
                return self_

            def __exit__(self_, *exc):
                return False

            def with_transaction(self_, fn):
                outer.sessions.append(self_)
                before = copy.deepcopy(outer.docs)
                try:
                    return fn(self_)
                except Exception:
                    outer.docs = before                         # the transaction aborts
                    raise
        return Session()


def mongo_sink(fake, strategy="STRATEGY_DOCUMENT"):
    from app.pipeline.sinks.mongo import MongoSink
    from app.pipeline.sinks.strategies.mongo import get_strategy

    sink = MongoSink.__new__(MongoSink)
    sink._client, sink._tenant_id = fake, "acme"
    sink._strategy, sink._strategy_name = get_strategy(strategy), strategy
    sink._collection_for_env = lambda env: fake
    return sink


SCOPE = {"_vt_tenant": "acme", "_vt_datasource": "ds", "_vt_env": "prod", "_vt_file": "app.yaml"}


def per_key_docs(n):
    return [{**SCOPE, "key": f"k{i}", "value": "v"} for i in range(n)]


def test_on_a_replica_set_the_document_and_the_replaced_shape_are_one_transaction():
    fake = FakeMongo(per_key_docs(3))
    out = mongo_sink(fake)._write(flat_data={"a": "1"}, labels=LABELS, data={"a": "1"}, **WRITE)
    assert out["replaced"] == 3 and len(fake.sessions) == 1
    assert [name for name, _ in fake.ops] == ["replace_one", "delete_many"]
    assert all(session is fake.sessions[0] for _, session in fake.ops)     # both operations are in it
    assert [d.get("data") for d in fake.docs] == [{"a": "1"}]


def test_a_failure_halfway_leaves_the_file_as_it_was():
    fake = FakeMongo(per_key_docs(3), fail_on="delete_many")
    before = copy.deepcopy(fake.docs)
    with pytest.raises(RuntimeError, match="delete_many failed"):
        mongo_sink(fake)._write(flat_data={"a": "1"}, labels=LABELS, data={"a": "1"}, **WRITE)
    assert fake.docs == before                  # the new document was written first and is gone again


def test_a_standalone_server_writes_without_a_session():
    fake = FakeMongo(per_key_docs(2), replica_set=False)
    mongo_sink(fake)._write(flat_data={"a": "1"}, labels=LABELS, data={"a": "1"}, **WRITE)
    assert fake.sessions == [] and all(session is None for _, session in fake.ops)


def test_whether_the_server_has_transactions_is_asked_once():
    fake = FakeMongo()
    sink = mongo_sink(fake)
    for _ in range(3):
        sink._write(flat_data={"a": "1"}, labels=LABELS, data={"a": "1"}, **WRITE)
    assert fake.asked == ["hello"]


def test_key_value_writes_are_not_wrapped_in_a_transaction():
    from app.pipeline.sinks.strategies.mongo import get_strategy

    assert get_strategy("STRATEGY_KEY_VALUE").transactional is False
    assert get_strategy("STRATEGY_FILE").transactional and get_strategy("STRATEGY_DOCUMENT").transactional
