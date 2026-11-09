"""
The watcher's drift check (CheckDatasource → app.ui.drift) is the only place
a sink is compared with Git. A sink that cannot be read must answer "can't
tell", never "every key is missing" — that would heal on every poll — and a
value the sink will not give back (a write-only sensitive value) is neither
missing nor changed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.sinks.base import UNREADABLE, Unreadable
from app.ui import views


def test_redis_read_failure_raises_only_in_strict_mode():
    from app.pipeline.sinks.redis import RedisSink

    sink = RedisSink.__new__(RedisSink)
    sink._redis = MagicMock(hmget=MagicMock(side_effect=ConnectionError("down")))
    sink._structure, sink._hash_key, sink._key_format = "HASH", "vartrack", "dot"
    sink._key_prefix = lambda ds, env: f"{env}:config"
    sink.strict_reads = False
    assert sink.read_values(["a.b"], "redis", "production") == {}
    sink.strict_reads = True
    with pytest.raises(ConnectionError):
        sink.read_values(["a.b"], "redis", "production")


RECORD = {"env": "production", "desired": {"A": "1", "B": "2", "TOKEN": "s"},
          "secretDigests": {}, "lastSync": {"taskId": "t1"}}


def test_compare_skips_unreadable_keys():
    with patch.object(views, "_read_current", return_value=({"A": "1", "B": "3", "TOKEN": UNREADABLE}, None)):
        result = views._compare_now({"datasource": "redis"}, "default", RECORD, ("default", "redis", "production"))
    states = {k["key"]: k["state"] for k in result["keys"]}
    assert states == {"A": "synced", "B": "drifted", "TOKEN": "unreadable"}
    assert result["differs"] == 1
    assert "liveValue" not in next(k for k in result["keys"] if k["key"] == "TOKEN")


def test_drift_ignore_keys_are_shown_but_not_counted():
    # Keys the application changes itself.
    rule = {"datasource": "redis", "drift_ignore": ["B", "TOK*"]}
    with patch.object(views, "_read_current", return_value=({"A": "1", "B": "3"}, None)):
        result = views._compare_now(rule, "default", RECORD, ("default", "redis", "production"))
    states = {k["key"]: k["state"] for k in result["keys"]}
    assert states == {"A": "synced", "B": "ignored", "TOKEN": "ignored"}
    assert result["differs"] == 0
    assert next(k for k in result["keys"] if k["key"] == "B")["liveValue"] == "3"


def test_an_unreadable_sink_is_unknown_not_drifted():
    from app.ui import drift

    rule = {"platform": "gitea", "datasource": "redis", "repositories": ["a/b"], "branch_map": {"main": "production"}}
    mgr = MagicMock(list_tenant_ids=MagicMock(return_value=["default"]), get_all_rules=MagicMock(return_value=[rule]))
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=mgr),
        patch.object(drift.ledger, "list_targets", return_value=[{**RECORD, "datasource": "redis"}]),
        patch.object(drift.ledger, "list_tasks", return_value=[]),
        patch.object(views, "_read_current", return_value=(None, "read failed: down")),
    ):
        status, drifted, message = drift.check_datasource("redis")
    assert (status, drifted) == (drift.UNKNOWN, [])
    assert "read failed" in message


def test_an_environment_no_branch_feeds_any_more_is_not_compared_or_called_drifted():
    """The rule changed after it wrote `main` and `staging`: only `production` is still its to keep in sync."""
    from app.ui import drift

    rule = {"platform": "gitea", "datasource": "redis", "repositories": ["a/b"], "branch_map": {"main": "production"}}
    mgr = MagicMock(list_tenant_ids=MagicMock(return_value=["default"]), get_all_rules=MagicMock(return_value=[rule]))
    records = [{**RECORD, "datasource": "redis", "env": e} for e in ("production", "main", "staging")]
    compared = []

    def compare(rule, tenant, record, fresh=False):
        compared.append(record["env"])
        # a leftover environment would differ from this rule's destination
        return {"error": None, "keys": [], "differs": 0 if record["env"] == "production" else 3}

    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=mgr),
        patch.object(drift.ledger, "list_targets", return_value=records),
        patch.object(drift.ledger, "list_tasks", return_value=[]),
        patch.object(views, "compare", compare),
    ):
        status, drifted, message = drift.check_datasource("redis")
    assert compared == ["production"]
    assert (status, drifted) == ("in_sync", [])
    assert message == "1 environment match Git"

