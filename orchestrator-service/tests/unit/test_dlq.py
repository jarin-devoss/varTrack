"""
tests/unit/test_dlq.py

A task that exhausts its retries goes to the dead-letter queue once. A dead
letter that fails again stays there: re-sending it would loop forever if
anything consumed the DLQ (a sync against an unreachable host did, every
twenty seconds).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.worker import celery as worker


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.celery, "send_task", lambda name, **kw: calls.append((name, kw.get("queue"))))
    monkeypatch.setattr("celery.current_app", worker.celery, raising=False)
    return calls


def _sender(retries, routing_key):
    return SimpleNamespace(name="app.tasks.etl.process_webhook_task", max_retries=3,
                           request=SimpleNamespace(retries=retries, delivery_info={"routing_key": routing_key}))


def test_a_task_that_exhausts_its_retries_is_dead_lettered(sent):
    worker._route_to_dlq(sender=_sender(3, "sync"), task_id="t1", exception=OSError("down"), kwargs={"a": 1})
    assert sent == [("app.tasks.etl.process_webhook_task", "dlq")]


def test_a_task_with_retries_left_is_not(sent):
    worker._route_to_dlq(sender=_sender(1, "sync"), task_id="t1", exception=OSError("down"))
    assert sent == []


def test_a_dead_letter_that_fails_again_is_not_sent_back(sent):
    worker._route_to_dlq(sender=_sender(3, "dlq"), task_id="t2", exception=OSError("down"))
    assert sent == []
