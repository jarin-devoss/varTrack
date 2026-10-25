"""
A rule with suspend: true takes no push and no heal; a sync started by hand
(web UI, vt) still runs.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tasks import etl


def _run(trigger: str, rule_config: dict):
    ctx = MagicMock(repo_url="http://gitea/acme/configs.git", ref="refs/heads/main", rule_config=rule_config)
    with (
        patch.object(etl, "run_stage_payload", return_value=ctx),
        patch("app.pipeline.repo_overrides.apply_repo_overrides", side_effect=RuntimeError("synced")) as overrides,
        patch.object(etl.process_webhook_task, "update_state"),
        patch("app.monitoring.tracer.start_webhook_span"),
    ):
        try:
            result = etl.process_webhook_task.run(
                platform="gitea", datasource="redis", raw_payload="{}",
                headers={etl.TRIGGER_HEADER: trigger}, received_at=0.0,
                rule_config=rule_config, tenant_id="default",
            )
        except RuntimeError:
            result = None
    return result, overrides.called


@pytest.mark.parametrize("trigger", ["webhook", "self_heal"])
def test_a_suspended_rule_skips_pushes_and_heals(trigger):
    result, synced = _run(trigger, {"suspend": True})
    assert not synced
    assert result["state"] == "SUCCEEDED"
    assert "suspended" in result["message"]


@pytest.mark.parametrize("trigger", ["ui", "cli"])
def test_a_sync_started_by_hand_still_runs(trigger):
    _, synced = _run(trigger, {"suspend": True})
    assert synced


def test_an_active_rule_syncs():
    _, synced = _run("webhook", {})
    assert synced


def test_the_drift_check_does_not_compare_a_suspended_rule():
    from app.ui import drift, views

    rule = {"platform": "gitea", "datasource": "redis", "repositories": ["a/b"], "suspend": True}
    mgr = MagicMock(list_tenant_ids=MagicMock(return_value=["default"]), get_all_rules=MagicMock(return_value=[rule]))
    with (
        patch("app.schema_registry.manager.get_schema_manager", return_value=mgr),
        patch.object(drift.ledger, "list_targets", return_value=[{"env": "production", "datasource": "redis"}]),
        patch.object(drift.ledger, "list_tasks", return_value=[]),
        patch.object(views, "compare") as compare,
    ):
        status, drifted, message = drift.check_datasource("redis")
    assert (status, drifted) == (drift.UNKNOWN, [])
    assert "suspended" in message
    compare.assert_not_called()
