"""
tests/unit/test_sync_limit_task.py

What the sync task does about a datasource's sync limit (app/tasks/etl.py: _take_slot, and the task around it):
it takes a slot before it writes, gives it back after, waits a few seconds and then goes back on the queue
when none is free, and fails after waiting as long as it is allowed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from app import dependencies
from app.pipeline import sync_slots
from app.pipeline.models import ETLFile, ETLResult, SyncResult
from app.pipeline.sync_slots import Limit, SlotWaitExpired
from app.tasks import etl

PROD = Limit(name="prod-mongo", datasources=("mongo",), max_at_once=2)


@pytest.fixture(autouse=True)
def _clean_registry():
    dependencies.reset()
    yield
    dependencies.reset()


# ── _take_slot ───────────────────────────────────────────────────────────────

class FakeTask:
    """The part of a bound celery task that _take_slot uses: how often it has been retried, and retry()."""

    def __init__(self, retries: int = 0) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.retried: dict | None = None

    def retry(self, countdown, max_retries):
        self.retried = {"countdown": countdown, "max_retries": max_retries}
        return Retry()


def _take(task, *, wait_for_slot, first=None, max_wait=600, metrics=None):
    # Settings are patched by dotted path: another test reloads app.config, so an imported `settings` can be a stale one.
    ui = MagicMock()
    with patch.object(sync_slots, "wait_for_slot", side_effect=wait_for_slot), \
            patch.object(sync_slots, "first_wait_at", return_value=first) as first_wait, \
            patch.object(sync_slots, "forget_wait") as forget, \
            patch("app.config.settings.SYNC_SLOT_MAX_WAIT_SECONDS", max_wait), \
            patch.object(etl, "ui_recorder", ui), \
            patch("app.monitoring.get_metrics", return_value=metrics):
        try:
            out = etl._take_slot(task, PROD, task_id="t1", tenant_id="acme", ui_task={"taskId": "t1"})
            return out, ui, first_wait, forget, None
        except BaseException as exc:                      # Retry, SlotWaitExpired, LockUnavailable
            return None, ui, first_wait, forget, exc


def test_a_free_slot_is_taken_with_no_wait_and_nothing_to_remember():
    slot = MagicMock()
    out, ui, first_wait, forget, err = _take(FakeTask(), wait_for_slot=lambda *a, **k: (slot, 1))
    assert err is None and out == (slot, 0.0)
    first_wait.assert_not_called()                 # nothing was waited for: nothing is written to Redis
    forget.assert_not_called()
    ui.task_waiting.assert_not_called()


def test_with_no_slot_free_the_task_says_so_and_goes_back_on_the_queue():
    task = FakeTask()

    def wait_for_slot(limit, holder, seconds, on_wait=None, **_):
        on_wait(2)                                  # none free: the first ask tells the caller
        return None, 2

    import time as _time
    out, ui, first_wait, forget, err = _take(task, wait_for_slot=wait_for_slot, first=_time.time() - 8)
    assert isinstance(err, Retry) and out is None
    assert task.retried["max_retries"] == 1000
    assert 1.0 <= task.retried["countdown"] <= 30.0
    ui.task_waiting.assert_called_once()
    assert ui.task_waiting.call_args.kwargs["message"] == "waiting for a slot of prod-mongo (2 of 2 in use)"
    forget.assert_not_called()                      # it is still waiting: its first ask is kept for the next try


def test_the_wait_a_task_waits_in_its_process_is_the_setting():
    seen = {}

    def wait_for_slot(limit, holder, seconds, **_):
        seen["seconds"] = seconds
        return None, 2

    with patch("app.config.settings.SYNC_SLOT_WAIT_SECONDS", 7):
        _take(FakeTask(), wait_for_slot=wait_for_slot, first=None)
    assert seen["seconds"] == 7


def test_a_task_back_from_the_queue_reports_how_long_it_waited_in_all():
    import time as _time
    slot = MagicMock()
    metrics = MagicMock()
    out, ui, first_wait, forget, err = _take(
        FakeTask(retries=3), wait_for_slot=lambda *a, **k: (slot, 2), first=_time.time() - 42, metrics=metrics)
    assert err is None
    assert out[0] is slot and 41 <= out[1] <= 44
    forget.assert_called_once_with("t1")
    metrics.inc_sync_slot_wait.assert_called_once_with("prod-mongo", "got")
    assert 41 <= metrics.observe_sync_slot_wait.call_args.args[1] <= 44


def test_a_task_that_got_its_slot_after_waiting_in_its_process_counts_as_got():
    import time as _time
    slot = MagicMock()
    metrics = MagicMock()

    def wait_for_slot(limit, holder, seconds, on_wait=None, **_):
        on_wait(2)
        return slot, 2

    out, *_rest, err = _take(FakeTask(), wait_for_slot=wait_for_slot, first=_time.time() - 1.5, metrics=metrics)
    assert err is None and 1.4 <= out[1] <= 1.7
    metrics.inc_sync_slot_wait.assert_called_once_with("prod-mongo", "got")


def test_a_task_that_has_waited_as_long_as_allowed_is_told_so():
    import time as _time
    metrics = MagicMock()
    out, ui, first_wait, forget, err = _take(
        FakeTask(retries=40), wait_for_slot=lambda *a, **k: (None, 2), first=_time.time() - 601, max_wait=600,
        metrics=metrics)
    assert isinstance(err, SlotWaitExpired)
    assert str(err).startswith("no slot of sync limit prod-mongo for 60") and str(err).endswith("(2 of 2 in use)")
    forget.assert_called_once_with("t1")
    metrics.inc_sync_slot_wait.assert_called_with("prod-mongo", "gave_up")


def test_a_task_that_has_waited_less_than_allowed_is_not_told_so():
    import time as _time
    task = FakeTask(retries=5)
    out, *_rest, err = _take(task, wait_for_slot=lambda *a, **k: (None, 2), first=_time.time() - 599, max_wait=600)
    assert isinstance(err, Retry)


def test_redis_that_cannot_be_asked_is_not_a_free_slot():
    def wait_for_slot(*a, **k):
        raise dependencies.LockUnavailable("sync limit prod-mongo cannot be checked: redis is down")

    out, *_rest, err = _take(FakeTask(), wait_for_slot=wait_for_slot)
    assert isinstance(err, dependencies.LockUnavailable)


def test_the_slots_in_use_are_reported_to_the_metrics():
    metrics = MagicMock()
    _take(FakeTask(), wait_for_slot=lambda *a, **k: (MagicMock(), 1), metrics=metrics)
    metrics.set_sync_slots.assert_called_once_with("prod-mongo", 1, 2)


# ── The task around it ───────────────────────────────────────────────────────

def _ctx():
    return MagicMock(repo_url="http://gitea/acme/configs.git", ref="refs/heads/main", branch="main",
                     commit_sha="abc", platform="gitea", rule_config={}, parsed_payload={})


def _etl_result() -> ETLResult:
    r = ETLResult()
    r.files.append(ETLFile(file_path="app.yaml", env="main", flat_data={"a": "1"}, root_key=""))
    return r


def _ok(path="app.yaml"):
    return SyncResult(file_path=path, env="main", status="ok", written=1)


def _run(*, limit=PROD, take=None, sync=None, retries=0):
    """process_webhook_task.run with the stages mocked; returns (result or exception, the mocks)."""
    order: list[str] = []
    slot = MagicMock()
    slot.release.side_effect = lambda: order.append("release")

    def run_stage_sync(**_):
        order.append("write")
        if isinstance(sync, BaseException):
            raise sync
        return sync if sync is not None else [_ok()]

    def default_take(*a, **k):
        order.append("take")
        return slot, 0.0

    recorder = MagicMock()
    take_slot = MagicMock(side_effect=take if take is not None else default_take)
    with (
        patch.object(etl, "run_stage_payload", return_value=_ctx()),
        patch("app.pipeline.repo_overrides.apply_repo_overrides", side_effect=lambda c, t=None: c),
        patch.object(etl, "run_stage_etl", return_value=_etl_result()),
        patch.object(etl, "run_stage_sync", side_effect=run_stage_sync),
        patch.object(etl, "ui_recorder", recorder),
        patch.object(etl, "_take_slot", take_slot),
        patch.object(etl.sync_slots, "limit_for", return_value=limit),
        patch.object(etl.process_webhook_task, "update_state"),
        patch("app.monitoring.tracer.start_webhook_span"),
    ):
        etl.process_webhook_task.push_request(id="t1", retries=retries)     # what a worker sets before it runs a task
        try:
            out = etl.process_webhook_task.run(
                platform="gitea", datasource="mongo", raw_payload="{}", headers={}, received_at=0.0,
                rule_config={}, tenant_id="default", dry_run=False)
        except BaseException as exc:
            out = exc
        finally:
            etl.process_webhook_task.pop_request()
    return out, SimpleNamespace(order=order, slot=slot, recorder=recorder, take_slot=take_slot)


def test_the_slot_is_taken_before_the_write_and_given_back_after():
    out, seen = _run()
    assert out["state"] == "SUCCEEDED"
    assert seen.order == ["take", "write", "release"]


def test_the_slot_is_given_back_when_the_write_fails():
    out, seen = _run(sync=RuntimeError("the store went away"))
    assert isinstance(out, RuntimeError)
    assert seen.order == ["take", "write", "release"]


def test_a_datasource_under_no_limit_takes_no_slot():
    out, seen = _run(limit=None)
    assert out["state"] == "SUCCEEDED" and seen.order == ["write"]
    seen.take_slot.assert_not_called()


def test_a_task_sent_back_to_the_queue_is_not_a_failure_and_is_not_recorded_as_one():
    out, seen = _run(take=Retry)
    assert isinstance(out, Retry)
    seen.recorder.task_errored.assert_not_called()
    assert seen.order == []                         # nothing was written


def test_a_task_that_waited_as_long_as_allowed_fails_saying_which_limit_and_how_full():
    expired = SlotWaitExpired(PROD, 2, 601)
    out, seen = _run(take=lambda *a, **k: (_ for _ in ()).throw(expired))
    assert out["state"] == "FAILED"
    assert out["message"] == "datasource mongo: no slot of sync limit prod-mongo for 601 s (2 of 2 in use)"
    seen.recorder.task_errored.assert_called_once()
    assert seen.order == []


def test_redis_that_cannot_be_asked_fails_the_task_as_a_redis_dependency():
    def unavailable(*a, **k):
        raise dependencies.LockUnavailable("sync limit prod-mongo cannot be checked: redis is down")

    out, seen = _run(take=unavailable)
    assert out["state"] == "FAILED" and out["message"].startswith("redis unavailable: sync limit prod-mongo")


def test_a_task_that_waited_says_how_long():
    slot = MagicMock()
    out, seen = _run(take=lambda *a, **k: (slot, 3.4))
    assert out["message"] == "completed (waited 3.4 s for a slot of prod-mongo)"


def test_a_task_that_barely_waited_says_nothing_of_it():
    out, seen = _run(take=lambda *a, **k: (MagicMock(), 0.2))
    assert out["message"] == "completed"


def test_the_task_says_which_files_an_older_commit_left_alone():
    skipped = SyncResult(file_path="app.yaml", env="main", status="skipped",
                         sync_note="skipped: commit bbbbbbbbbbbb is newer than aaaaaaaaaaaa and is already written to main")
    out, _ = _run(sync=[skipped])
    assert out["state"] == "SUCCEEDED"
    assert out["message"] == "completed; " + skipped.sync_note
    assert out["files"][0]["status"] == "skipped"


def test_a_task_back_from_the_queue_is_recorded_as_resumed_not_started_again():
    out, seen = _run(retries=2)
    assert seen.recorder.task_started.call_args.kwargs["resumed"] is True
    out, seen = _run(retries=0)
    assert seen.recorder.task_started.call_args.kwargs["resumed"] is False
