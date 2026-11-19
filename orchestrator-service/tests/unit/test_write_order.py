"""
tests/unit/test_write_order.py

An older commit never overwrites a newer one (app/pipeline/write_order.py, and where stage_sync uses it).
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

for _m in ("pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import app.pipeline.sinks  # noqa: E402,F401 — pre-import for patch() resolution

from app.pipeline import write_order  # noqa: E402
from app.pipeline.models import ETLFile, ETLResult, PayloadContext  # noqa: E402
from app.tasks.payload import PruneConfig  # noqa: E402
from tests.unit._fakes import FakeRedis  # noqa: E402

A = "a" * 40          # an older commit
B = "b" * 40          # a newer one: a descendant of A
C = "c" * 40          # newer still
REPO = "https://git.example/acme/configs.git"
RULE: dict = {}


@pytest.fixture
def redis():
    return FakeRedis()


class History:
    """A linear history A < B < C, as git's `merge-base --is-ancestor` would answer for it."""
    order = [A, B, C]

    def __init__(self, known=(A, B, C)):
        self.known = set(known)
        self.fetched: list[str] = []
        self.compared: list[tuple[str, str]] = []

    def has_commit(self, url, sha):
        return sha in self.known

    def fetch(self, url, token, sha=None, ref=None):
        self.fetched.append(sha)
        self.known.add(sha)

    def is_ancestor(self, url, ancestor, descendant):
        self.compared.append((ancestor, descendant))
        return self.order.index(ancestor) <= self.order.index(descendant)


@pytest.fixture
def history():
    h = History()
    with patch("app.utils.git.mirror.get_default_mirrors", return_value=h), \
            patch("app.pipeline.git_credentials.credential_for", return_value=None):
        yield h


def _newer(redis, commit, *, file="app.yaml", place="main"):
    return write_order.newer_commit_written(datasource="mongo", place=place, repo_url=REPO, file_path=file,
                                            commit_sha=commit, rule_config=RULE, client=redis)


def _record(redis, commit, *, file="app.yaml", place="main"):
    write_order.record_written(datasource="mongo", place=place, repo_url=REPO, file_path=file, commit_sha=commit,
                               client=redis)


# ── The module ───────────────────────────────────────────────────────────────

def test_with_nothing_written_before_there_is_nothing_newer(redis, history):
    assert _newer(redis, A) is None
    assert history.compared == []


def test_an_older_commit_is_told_which_newer_one_is_written(redis, history):
    _record(redis, B)
    assert _newer(redis, A) == B
    assert history.compared == [(A, B)]


def test_the_same_commit_is_written_again(redis, history):
    _record(redis, B)
    assert _newer(redis, B) is None
    assert history.compared == []               # nothing to compare


def test_a_newer_commit_is_written_over_an_older_one(redis, history):
    _record(redis, A)
    assert _newer(redis, C) is None


def test_unrelated_history_is_written(redis):
    # A force push: the commit written is not an ancestor of the one that arrives, nor the other way round.
    class Unrelated(History):
        def is_ancestor(self, url, ancestor, descendant):
            return False

    with patch("app.utils.git.mirror.get_default_mirrors", return_value=Unrelated()), \
            patch("app.pipeline.git_credentials.credential_for", return_value=None):
        _record(redis, B)
        assert _newer(redis, A) is None


def test_a_cli_label_is_not_a_commit_and_is_never_ordered(redis, history):
    redis.set("any", "x")
    assert _newer(redis, "cli-push") is None
    _record(redis, "cli-push")
    assert not any(k.startswith(write_order.KEY_PREFIX) for k in redis.values)
    assert history.compared == []


def test_the_record_is_per_file(redis, history):
    _record(redis, B, file="other.yaml")
    assert _newer(redis, A, file="app.yaml") is None            # B wrote another file: A's file is not older
    assert _newer(redis, A, file="other.yaml") == B


def test_the_record_is_per_place(redis, history):
    _record(redis, B, place="production")
    assert _newer(redis, A, place="staging") is None


def test_the_record_is_per_datasource(redis, history):
    write_order.record_written(datasource="mongo-dr", place="main", repo_url=REPO, file_path="app.yaml",
                               commit_sha=B, client=redis)
    assert _newer(redis, A) is None                              # "mongo" never held B


def test_a_commit_the_mirror_does_not_have_is_fetched_once_and_then_compared(redis):
    h = History(known=(A,))
    with patch("app.utils.git.mirror.get_default_mirrors", return_value=h), \
            patch("app.pipeline.git_credentials.credential_for", return_value=None):
        _record(redis, B)
        assert _newer(redis, A) == B
    assert h.fetched == [B]


def test_a_comparison_that_cannot_be_made_writes_and_says_so(redis, caplog):
    class Broken(History):
        def is_ancestor(self, url, ancestor, descendant):
            raise RuntimeError("git merge-base could not run")

    with patch("app.utils.git.mirror.get_default_mirrors", return_value=Broken()), \
            patch("app.pipeline.git_credentials.credential_for", return_value=None):
        _record(redis, B)
        with caplog.at_level("WARNING", logger="app.pipeline.write_order"):
            assert _newer(redis, A) is None
    assert "writing" in " ".join(r.getMessage() for r in caplog.records)


def test_a_fetch_that_fails_writes_too(redis):
    class NoFetch(History):
        def fetch(self, url, token, sha=None, ref=None):
            raise RuntimeError("commit not found")

    with patch("app.utils.git.mirror.get_default_mirrors", return_value=NoFetch(known=(A,))), \
            patch("app.pipeline.git_credentials.credential_for", return_value=None):
        _record(redis, B)
        assert _newer(redis, A) is None


def test_redis_that_does_not_answer_means_write(redis, history):
    _record(redis, B)
    redis.fail = True
    assert _newer(redis, A) is None
    _record(redis, C)                                            # and recording does not raise


def test_a_record_that_is_not_a_commit_is_ignored(redis, history):
    redis.values[write_order._key("mongo", "main", REPO, "app.yaml")] = "not-a-commit"
    assert _newer(redis, A) is None


# ── Where stage_sync uses it ─────────────────────────────────────────────────

def _file(path="app.yaml", env="main") -> ETLFile:
    return ETLFile(file_path=path, env=env, flat_data={"a.b": "1", "c": "2"}, root_key="vartrack")


def _etl(*files) -> ETLResult:
    r = ETLResult()
    r.files.extend(files)
    return r


def _sync(redis, history, commit, *files, sink=None):
    sink = sink or MagicMock()
    if not isinstance(sink.write.side_effect, list):
        sink.write.return_value = {"written": 2, "pruned": 0}
    ctx = PayloadContext(platform="gitea", repo_url=REPO, ref="refs/heads/main", branch="main", commit_sha=commit,
                         tag=None, pr_number=None, rule_config={"mongo_uri": "mongodb://x", "database": "v"},
                         parsed_payload={})
    with patch("app.pipeline.sinks.create_sink", return_value=sink), \
            patch("app.pipeline.stage_sync.extract_prune_config", return_value=PruneConfig(enabled=False)), \
            patch("app.pipeline.write_order._client", return_value=redis):
        from app.pipeline.stage_sync import run_stage_sync
        return run_stage_sync(ctx=ctx, etl=_etl(*files), datasource="mongo", tenant_id="acme", total_sources=1), sink


def _held(redis):
    return {k: v for k, v in redis.values.items() if k.startswith(write_order.KEY_PREFIX)}


def test_a_sync_that_writes_remembers_its_commit(redis, history):
    results, sink = _sync(redis, history, B, _file())
    assert [r.status for r in results] == ["ok"]
    assert sink.write.call_count == 1
    assert list(_held(redis).values()) == [B]


def test_an_older_commit_arriving_second_writes_nothing_and_says_why(redis, history):
    _sync(redis, history, B, _file())
    results, sink = _sync(redis, history, A, _file())
    assert [r.status for r in results] == ["skipped"]
    assert results[0].sync_note == f"skipped: commit {B[:12]} is newer than {A[:12]} and is already written to main"
    assert sink.write.call_count == 0
    assert list(_held(redis).values()) == [B]                   # the newer commit is still the one on record


def test_a_newer_commit_arriving_second_is_written(redis, history):
    _sync(redis, history, A, _file())
    results, sink = _sync(redis, history, C, _file())
    assert [r.status for r in results] == ["ok"] and sink.write.call_count == 1
    assert list(_held(redis).values()) == [C]


def test_the_same_commit_twice_writes_twice(redis, history):
    _sync(redis, history, B, _file())
    results, sink = _sync(redis, history, B, _file())
    assert [r.status for r in results] == ["ok"] and sink.write.call_count == 1


def test_only_the_file_a_newer_commit_wrote_is_skipped(redis, history):
    _sync(redis, history, B, _file("app.yaml"))                 # B wrote app.yaml only
    results, sink = _sync(redis, history, A, _file("app.yaml"), _file("other.yaml"))
    assert {r.file_path: r.status for r in results} == {"app.yaml": "skipped", "other.yaml": "ok"}
    assert [c.kwargs["file_path"] for c in sink.write.call_args_list] == ["other.yaml"]


def test_a_skipped_file_is_not_handed_to_prune(redis, history):
    # Prune deletes what the old inventory had and this sync's files do not: counting the older commit's file
    # as this sync's would delete the keys the newer commit added. It stays out, so its keys are kept.
    _sync(redis, history, B, _file())
    with patch("app.pipeline.stage_sync._prune_environment") as prune:
        _sync(redis, history, A, _file())
    prune.assert_not_called()


def test_a_sync_that_fails_leaves_the_record_as_it_was(redis, history):
    _sync(redis, history, A, _file("a.yaml"), _file("b.yaml"))
    assert set(_held(redis).values()) == {A}
    failing = MagicMock()
    failing.write.side_effect = [{"written": 2, "pruned": 0}, RuntimeError("the store went away")]
    results, _ = _sync(redis, history, C, _file("a.yaml"), _file("b.yaml"), sink=failing)
    assert [r.file_path for r in results if r.status == "error"] == ["b.yaml"]     # the second file failed
    assert set(_held(redis).values()) == {A}                    # C was not recorded: it was not written as a whole


def test_a_cli_push_with_a_label_is_written_and_not_ordered(redis, history):
    results, sink = _sync(redis, history, "cli-push", _file())
    assert [r.status for r in results] == ["ok"] and sink.write.call_count == 1
    assert _held(redis) == {}


def test_an_older_commit_is_counted_in_the_metrics(redis, history):
    _sync(redis, history, B, _file())
    metrics = MagicMock()
    with patch("app.monitoring.get_metrics", return_value=metrics):
        _sync(redis, history, A, _file())
    metrics.inc_older_commit_skipped.assert_called_once_with("mongo")
