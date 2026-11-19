"""
tests/unit/test_recorder_waiting.py

A sync waiting for a slot of a sync limit is on its page as waiting, and one that comes back from the queue is
not a sync that starts again (app/ui/recorder.py).
"""
from __future__ import annotations

from unittest.mock import patch

from app.ui import recorder

START = dict(tenant="acme", platform="gitea", datasource="mongo", trigger="webhook",
             repo_url="http://gitea/acme/configs.git", ref="main", revision="abc", dry_run=False)


def test_a_sync_that_starts_leaves_a_webhook_event_and_a_started_event():
    with patch.object(recorder, "ledger") as ledger:
        ledger.repo_name.return_value = "acme/configs"
        ledger.now_iso.return_value = "2026-11-19T10:00:00Z"
        recorder.task_started("t1", **START)
    reasons = [c.kwargs["reason"] for c in ledger.record_event.call_args_list]
    assert reasons == ["WebhookReceived", "SyncStarted"]


def test_a_sync_that_comes_back_from_the_queue_leaves_no_second_started_event():
    with patch.object(recorder, "ledger") as ledger:
        ledger.repo_name.return_value = "acme/configs"
        ledger.now_iso.return_value = "2026-11-19T10:00:00Z"
        task = recorder.task_started("t1", resumed=True, **START)
    ledger.record_event.assert_not_called()
    ledger.record_task.assert_called_once()           # its page is running again
    assert task["state"] == "RUNNING" and task["message"] == "running"


def test_a_waiting_sync_says_what_it_waits_for_and_stays_running():
    task = {"taskId": "t1", "state": "RUNNING", "message": "running"}
    with patch.object(recorder, "ledger") as ledger:
        recorder.task_waiting(task, tenant="acme", message="waiting for a slot of prod-mongo (2 of 2 in use)")
    assert task["message"] == "waiting for a slot of prod-mongo (2 of 2 in use)"
    assert task["state"] == "RUNNING"                 # an active task: the watcher waits on it, as on any running sync
    ledger.record_task.assert_called_once_with(task, tenant="acme")


def test_a_sync_with_no_record_has_nothing_to_say_about_waiting():
    with patch.object(recorder, "ledger") as ledger:
        recorder.task_waiting(None, tenant="acme", message="waiting")
    ledger.record_task.assert_not_called()


def test_a_recorder_that_cannot_write_does_not_stop_the_sync():
    task = {"taskId": "t1"}
    with patch.object(recorder, "ledger") as ledger:
        ledger.record_task.side_effect = ConnectionError("redis is down")
        recorder.task_waiting(task, tenant="acme", message="waiting")     # does not raise
