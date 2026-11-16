"""
tests/unit/test_files_let_go.py

A rule that stops reading a file lets it go. The environment's desired state is the files its rule names now: a complete
sync drops the entry of a file the rule no longer reads (and prunes its keys, or its document, when the rule says prune);
a sync that wrote only some of the files changes nothing. What is left in the sink is shown as "not in Git" and is not
drift, and the sync says what it let go.
"""
# ruff: noqa: F811  (the `redis` fixture is imported from test_sync_ownership and then named as a parameter)
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.pipeline import repo_scope, stage_sync
from app.pipeline.models import ETLResult
from app.pipeline.sinks.base import UNREADABLE
from app.ui import ledger, views
from tests.unit.test_sync_ownership import (  # noqa: F401  (redis is a fixture)
    DS, REPO, TENANT, FakeSink, _file, _sink_class, _sync, _target, redis,
)

READS_B = {"file_name": "b.yaml"}


def _events(redis, reason):
    return [c.kwargs for c in redis.events.call_args_list if c.kwargs.get("reason") == reason]


def _task_message(results, **rule):
    """The message the task shows for a sync (what _sync recorded): from the last recorded task."""
    return ledger.record_task.call_args_list[-1].args[0]["message"] if ledger.record_task.call_args_list else ""


# ── files_of: the files the rule reads for an environment ─────────────────────

@pytest.mark.parametrize("rule, env, expected", [
    ({"file_name": "app.yaml"}, "production", {"app.yaml"}),
    ({"file_name": "app.yaml", "file_path_map": {"production": "p.yaml"}}, "production", {"app.yaml"}),   # file_name wins
    ({"file_path_map": {"production": "p.yaml", "staging": "s.yaml"}}, "staging", {"s.yaml"}),
    ({"file_path_map": {"production": "p.yaml"}}, "qa", None),                                    # not one of its entries
    ({"file_path_map": {"production": "p.yaml"}, "branch_map": {"main": "production"}}, "production", None),
    ({"branch_map": {"main": "production"}}, "production", None),                                 # names no file
    ({}, "production", None),
])
def test_files_of(rule, env, expected):
    assert repo_scope.files_of(rule, env) == expected


# ── The ledger: a complete sync lets go the files the rule no longer reads ────

def test_a_file_the_rule_stopped_reading_leaves_the_desired_state_and_its_keys_are_pruned(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1", "shared": "s"})], rule_extra={"file_name": "a.yaml"})
    assert set(_target("production")["desiredByFile"]) == {"acme/configs:a.yaml"}

    results = _sync(sink, [_file("b.yaml", "production", **{"b.only": "2", "shared": "s"})], rule_extra=READS_B)

    target = _target("production")
    assert set(target["desiredByFile"]) == {"acme/configs:b.yaml"}
    assert set(target["desired"]) == {"b.only", "shared"}
    assert "letGo" not in target                       # everything it wrote that nothing holds was pruned
    assert sink.store["production"] == {"b.only": "2", "shared": "s"}
    assert sink.deleted == [("production", ["a.only"])]
    (group,) = results[-1].let_go
    assert group["file"] == "a.yaml" and group["pruned"] == ["a.only"] and group["left"] == [] and group["prune"] is True


def test_the_task_and_an_event_say_what_was_let_go(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1", "shared": "s"})], rule_extra={"file_name": "a.yaml"})
    _sync(sink, [_file("b.yaml", "production", **{"b.only": "2", "shared": "s"})], rule_extra=READS_B)

    (event,) = _events(redis, "FileLetGo")
    assert event["env"] == "production" and event["rule"] == "github/redis"
    assert event["message"] == "a.yaml is no longer read by github/redis for production: let go; 1 key it wrote was pruned"
    assert event["message"] in _task_message(None)


def test_without_prune_the_keys_stay_in_the_sink_and_are_noted_not_desired(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1", "shared": "s"})], prune=False, rule_extra={"file_name": "a.yaml"})

    results = _sync(sink, [_file("b.yaml", "production", **{"b.only": "2", "shared": "s"})], prune=False, rule_extra=READS_B)

    target = _target("production")
    assert set(target["desired"]) == {"b.only", "shared"}
    assert sink.store["production"] == {"a.only": "1", "b.only": "2", "shared": "s"}      # nothing deleted
    assert target["letGo"] == {"acme/configs:a.yaml": {
        "file": "a.yaml", "taskId": "t1", "at": target["letGo"]["acme/configs:a.yaml"]["at"],
        "keys": {"a.only": {"value": "1", "digest": None}}}}
    assert results[-1].let_go[0]["left"] == ["a.only"]
    (event,) = _events(redis, "FileLetGo")
    assert event["message"].endswith("1 key it wrote is left in the sink (prune is off)")


def test_only_the_keys_the_sink_holds_are_said_to_be_left(redis):
    """A rule that also moved the environment elsewhere leaves nothing at the new place."""
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1"})], prune=False, rule_extra={"file_name": "a.yaml"})
    sink.store["production"].clear()                      # the new destination holds none of a.yaml's keys

    results = _sync(sink, [_file("b.yaml", "production", **{"b.only": "2"})], prune=False, rule_extra=READS_B)

    assert results[-1].let_go[0]["left"] == []
    assert "letGo" not in _target("production")
    (event,) = _events(redis, "FileLetGo")
    assert event["message"].endswith("nothing it wrote is left in the sink")


def test_a_sync_that_wrote_only_some_of_the_files_lets_nothing_go(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1"})], rule_extra={"file_name": "a.yaml"})

    failing = _sink_class()(store=sink.store, fail_write_on="b.yaml")
    _sync(failing, [_file("b.yaml", "production", **{"b.only": "2"})], rule_extra=READS_B)

    assert set(_target("production")["desiredByFile"]) == {"acme/configs:a.yaml"}      # the failed sync changed nothing
    assert _events(redis, "FileLetGo") == []
    assert sink.store["production"] == {"a.only": "1"}


def test_a_sync_of_other_environments_files_lets_nothing_go(redis):
    """The rule reads a file for each environment: a push to staging says nothing about production's."""
    sink = _sink_class()()
    rule = {"file_path_map": {"production": "p.yaml", "staging": "s.yaml"}}
    _sync(sink, [_file("p.yaml", "production", **{"p": "1"}), _file("s.yaml", "staging", **{"s": "1"})], rule_extra=rule)
    _sync(sink, [_file("s.yaml", "staging", **{"s": "2"})], rule_extra=rule)

    assert set(_target("production")["desiredByFile"]) == {"acme/configs:p.yaml"}
    assert _events(redis, "FileLetGo") == []


def test_a_rule_with_no_known_file_lets_nothing_go(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a": "1"})])
    _sync(sink, [_file("b.yaml", "production", **{"b": "2"})])             # a branch_map-only rule: its files are not known

    assert set(_target("production")["desiredByFile"]) == {"acme/configs:a.yaml", "acme/configs:b.yaml"}


def test_a_file_another_repository_fed_is_not_this_syncs_to_let_go(redis):
    sink = _sink_class()()
    one, two = "http://gitea:3000/acme/one.git", "http://gitea:3000/acme/two.git"
    _sync(sink, [_file("a.yaml", "production", **{"a": "1"})], repo=one, rule_extra={"file_name": "a.yaml"})
    _sync(sink, [_file("b.yaml", "production", **{"b": "2"})], repo=two, rule_extra=READS_B)

    # two's sync is complete for b.yaml; one's a.yaml is not a file of two, so it stays until one's own sync
    names = set(_target("production")["desiredByFile"])
    assert names == {"acme/one:a.yaml", "acme/two:b.yaml"}
    _sync(sink, [_file("b.yaml", "production", **{"b": "2"})], repo=one, rule_extra=READS_B)
    assert set(_target("production")["desiredByFile"]) == {"acme/two:b.yaml", "acme/one:b.yaml"}


def test_a_file_written_again_is_not_left_behind(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1"})], prune=False, rule_extra={"file_name": "a.yaml"})
    _sync(sink, [_file("b.yaml", "production", **{"b.only": "2"})], prune=False, rule_extra=READS_B)
    assert "a.only" in _target("production")["letGo"]["acme/configs:a.yaml"]["keys"]

    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1"})], prune=False, rule_extra={"file_name": "a.yaml"})

    target = _target("production")
    assert "a.only" in target["desired"]
    assert list(target["letGo"]) == ["acme/configs:b.yaml"]            # a.yaml is read again; b.yaml is the one let go now


def test_a_secret_is_noted_by_its_digest(redis):
    sink = _sink_class()()
    first = _file("a.yaml", "production", TOKEN="s3cret")
    first.secret_fields = frozenset({"TOKEN"})
    _sync(sink, [first], prune=False, rule_extra={"file_name": "a.yaml"})
    _sync(sink, [_file("b.yaml", "production", **{"b": "2"})], prune=False, rule_extra=READS_B)

    held = _target("production")["letGo"]["acme/configs:a.yaml"]["keys"]["TOKEN"]
    assert held["value"] == ledger.SECRET_MASK and held["digest"] == ledger.secret_digest("s3cret")


# ── A sink that keeps each file as a document ─────────────────────────────────

def _document_sink_class():
    """A sink that stores a file as one document, like MongoDB's DOCUMENT strategy: prunes files, not keys."""
    class Sink(_sink_class(prunes=False)):
        deleted_files = None

        @classmethod
        def prunes_files(cls, settings, rule_config):
            return True

        def delete_file_data(self, datasource, env, file_path):
            if self.deleted_files is None:
                self.deleted_files = []
            self.deleted_files.append((env, file_path))
            return 1
    return Sink


def test_prune_deletes_the_document_of_a_file_the_rule_stopped_reading(redis):
    sink = _document_sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1", "shared": "s"})], rule_extra={"file_name": "a.yaml"})

    results = _sync(sink, [_file("b.yaml", "production", **{"b.only": "2", "shared": "s"})], rule_extra=READS_B)

    assert sink.deleted_files == [("production", "a.yaml")]
    (group,) = results[-1].let_go
    assert group["document"] is True and group["left"] == []
    assert "letGo" not in _target("production")
    (event,) = _events(redis, "FileLetGo")
    assert event["message"].endswith("its document was deleted")


def test_without_prune_the_document_stays_and_its_keys_are_noted(redis):
    sink = _document_sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1"})], prune=False, rule_extra={"file_name": "a.yaml"})
    _sync(sink, [_file("b.yaml", "production", **{"b.only": "2"})], prune=False, rule_extra=READS_B)

    assert sink.deleted_files is None
    assert list(_target("production")["letGo"]["acme/configs:a.yaml"]["keys"]) == ["a.only"]


def test_a_document_changed_outside_varTrack_is_kept_whole_and_the_reason_said(redis):
    sink = _document_sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1"})], rule_extra={"file_name": "a.yaml"})
    sink.store["production"]["a.only"] = "changed by hand"

    results = _sync(sink, [_file("b.yaml", "production", **{"b.only": "2"})], rule_extra=READS_B)

    assert sink.deleted_files is None
    assert "changed outside varTrack: a.only" in results[-1].let_go[0]["keptBecause"]
    assert results[-1].let_go[0]["left"] == ["a.only"]
    (event,) = _events(redis, "FileLetGo")
    assert "its document was kept: changed outside varTrack: a.only" in event["message"]


def test_a_document_holding_a_protected_key_is_kept_whole(redis):
    sink = _document_sink_class()()
    rule = {"file_name": "a.yaml", "prune": {"enabled": True, "protection": ["keep.*"]}}
    _sync(sink, [_file("a.yaml", "production", **{"keep.me": "1", "a.only": "2"})], rule_extra=rule)

    results = _sync(sink, [_file("b.yaml", "production", **{"b.only": "3"})],
                    rule_extra={"file_name": "b.yaml", "prune": {"enabled": True, "protection": ["keep.*"]}})

    assert sink.deleted_files is None
    assert "protected" in results[-1].let_go[0]["keptBecause"]


# ── Reading: only the files the rule names now ────────────────────────────────

def _record(**entries):
    """A target holding one entry per (path, {key: value}) and its merged desired state."""
    by_file = {f"acme/configs:{path}": {"file": path, "desired": desired, "secretDigests": {}}
               for path, desired in entries.items()}
    desired, digests, files = ledger.merge_entries(by_file)
    return {"env": "production", "datasource": DS, "desired": desired, "secretDigests": digests,
            "desiredByFile": by_file, "files": files, "lastSync": {"taskId": "t1"}}


def test_the_record_is_read_through_the_files_the_rule_names_now():
    record = _record(**{"a.yaml": {"a": "1"}, "b.yaml": {"b": "2"}})

    seen = views.current_record({"file_name": "b.yaml"}, record)

    assert seen["desired"] == {"b": "2"} and seen["files"] == ["b.yaml"] and list(seen["desiredByFile"]) == ["acme/configs:b.yaml"]
    assert record["desired"] == {"a": "1", "b": "2"}                                       # the record itself is untouched
    assert views.current_record({"file_name": "a.yaml"}, _record(**{"a.yaml": {"a": "1"}})) is not None
    unchanged = _record(**{"b.yaml": {"b": "2"}})
    assert views.current_record({"file_name": "b.yaml"}, unchanged) is unchanged            # nothing to hide: the same record
    assert views.current_record({"branch_map": {"main": "production"}}, record) is record   # files not known: nothing hidden


def test_an_environment_whose_file_was_never_synced_reads_as_not_synced_yet():
    rule = {"platform": "github", "datasource": DS, "file_name": "new.yaml", "branch_map": {"main": "production"},
            "repo_url": REPO}
    record = {**_record(**{"old.yaml": {"a": "1"}}), "repository": "acme/configs"}
    with patch.object(views, "compare") as compare:
        view = views.rule_view(rule, TENANT, [record], [])
    compare.assert_not_called()                                                             # nothing to compare with
    assert [(t["env"], t["drift"]) for t in view["targets"]] == [("production", "missing")]


def test_an_environment_reads_synced_when_only_a_file_the_rule_dropped_differs():
    """The case that failed 8 browser specs: the old file's keys were still desired and missing from the sink."""
    rule = {"datasource": DS, "file_name": "app.yaml", "branch_map": {"main": "production"}}
    record = _record(**{"app.yaml": {"x": "1"}, "env-keyed/old.yaml": {"old.only": "9"}})
    with patch.object(views, "_read_current", return_value=({"x": "1"}, None)) as read:
        result = views.compare(rule, TENANT, record, fresh=True)
    assert result["differs"] == 0 and [k["state"] for k in result["keys"]] == ["synced"]
    assert read.call_args.args[3] == ["x"]                                                  # the dropped file's keys are not even read


def test_keys_a_dropped_file_left_in_the_sink_are_not_in_git_and_not_drift():
    rule = {"datasource": DS, "file_name": "b.yaml"}
    record = {**_record(**{"b.yaml": {"b": "2"}}),
              "letGo": {"acme/configs:a.yaml": {"file": "a.yaml", "taskId": "t0", "at": "x",
                                                "keys": {"a.only": {"value": "1", "digest": None},
                                                         "TOKEN": {"value": "***", "digest": "d"}}}}}
    live = {"b": "2", "a.only": "1", "TOKEN": "s3cret"}
    with patch.object(views, "_read_current", return_value=(live, None)):
        result = views.compare(rule, TENANT, record, fresh=True)

    assert result["differs"] == 0
    states = {k["key"]: k for k in result["keys"]}
    assert states["a.only"]["state"] == "unmanaged" and states["a.only"]["sourceFile"] == "a.yaml"
    assert states["a.only"]["reason"] == "no longer read by the rule" and "gitValue" not in states["a.only"]
    assert states["a.only"]["liveValue"] == "1"
    assert states["TOKEN"]["secret"] is True and states["TOKEN"]["liveValue"] == ledger.SECRET_MASK


def test_a_left_behind_key_that_is_gone_from_the_sink_is_noted_no_more():
    rule = {"datasource": DS, "file_name": "b.yaml"}
    record = {**_record(**{"b.yaml": {"b": "2"}}),
              "letGo": {"acme/configs:a.yaml": {"file": "a.yaml", "taskId": "t0", "at": "x",
                                                "keys": {"a.only": {"value": "1", "digest": None},
                                                         "a.write-only": {"value": "2", "digest": None}}}}}
    with patch.object(views, "_read_current", return_value=({"b": "2", "a.write-only": UNREADABLE}, None)), \
         patch.object(ledger, "settle_let_go") as settle:
        result = views.compare(rule, TENANT, record, fresh=True)

    settle.assert_called_once_with(tenant=TENANT, datasource=DS, env="production", gone=["a.only"])
    assert [k["key"] for k in result["keys"]] == ["b"]                                      # a write-only key is not shown as left


def test_settle_let_go_forgets_the_keys_and_then_the_entry(redis):
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.1": "1", "a.2": "2"})], prune=False, rule_extra={"file_name": "a.yaml"})
    _sync(sink, [_file("b.yaml", "production", **{"b": "3"})], prune=False, rule_extra=READS_B)

    ledger.settle_let_go(tenant=TENANT, datasource=DS, env="production", gone=["a.1"])
    assert list(_target("production")["letGo"]["acme/configs:a.yaml"]["keys"]) == ["a.2"]
    ledger.settle_let_go(tenant=TENANT, datasource=DS, env="production", gone=["a.2"])
    assert "letGo" not in _target("production")


# ── A dry run says what a sync would find stale ───────────────────────────────

def test_a_dry_run_lists_the_keys_of_a_file_the_rule_no_longer_reads(redis):
    cls = _sink_class()
    sink = cls()
    _sync(sink, [_file("a.yaml", "production", **{"a.only": "1", "shared": "s"})], rule_extra={"file_name": "a.yaml"})
    etl = ETLResult()
    etl.files.append(_file("b.yaml", "production", **{"b.only": "2", "shared": "s"}))

    rule = {"datasource": DS, "prune": True, **READS_B}
    with patch("app.pipeline.sinks.sink_class", return_value=cls):
        candidates = stage_sync.prune_candidates(etl, DS, TENANT, rule, REPO)

    assert candidates == {"production": ["a.only"]}


def test_the_let_go_files_are_counted(redis):
    metrics = type("M", (), {"files": [], "keys": [],
                             "inc_files_let_go": lambda self, ds, n: self.files.append((ds, n)),
                             "inc_keys_left": lambda self, ds, n: self.keys.append((ds, n)),
                             "inc_sink_pruned": lambda self, *a: None})()
    sink = _sink_class()()
    _sync(sink, [_file("a.yaml", "production", **{"a.1": "1", "a.2": "2"})], prune=False, rule_extra={"file_name": "a.yaml"})
    with patch("app.pipeline.stage_sync._m", return_value=metrics):
        _sync(sink, [_file("b.yaml", "production", **{"b": "3"})], prune=False, rule_extra=READS_B)
    assert metrics.files == [(DS, 1)] and metrics.keys == [(DS, 2)]
