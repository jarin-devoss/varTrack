"""
tests/unit/test_worker_warm_start.py

A pool process has what a sync needs before it takes one (app/worker/warm.py): the worker's main process preloads
the sinks' client libraries before it forks the pool, and each new pool process loads the tenants' schemas.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.worker import warm


@pytest.fixture
def manager():
    m = MagicMock()
    m.list_tenant_ids.return_value = ["alpha", "beta"]
    with patch("app.schema_registry.manager.get_schema_manager", return_value=m):
        yield m


def test_every_tenant_with_a_schema_repository_is_loaded(manager):
    assert warm.warm_schemas(5) is True
    assert [c.args[0] for c in manager.get_or_clone.call_args_list] == ["alpha", "beta"]


def test_the_load_is_said_with_its_count_and_time(manager, caplog):
    with caplog.at_level("INFO", logger="app.worker.warm"):
        warm.warm_schemas(5)
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "the schemas of 2 of 2 tenant(s) loaded in" in said


def test_a_tenant_that_fails_is_said_and_the_others_are_still_loaded(manager, caplog):
    def get_or_clone(tenant):
        if tenant == "alpha":
            raise RuntimeError("clone of https://bot:hunter2@git.example/acme/schemas.git failed")

    manager.get_or_clone.side_effect = get_or_clone
    with caplog.at_level("INFO", logger="app.worker.warm"):
        assert warm.warm_schemas(5) is True            # the warm start is over: it did what it could
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "tenant alpha could not be loaded" in said
    assert "the schemas of 1 of 2 tenant(s) loaded" in said
    assert "hunter2" not in said                        # a clone URL's password is never logged
    assert [c.args[0] for c in manager.get_or_clone.call_args_list] == ["alpha", "beta"]


def test_a_manager_that_cannot_list_its_tenants_does_not_stop_the_process(manager, caplog):
    manager.list_tenant_ids.side_effect = RuntimeError("config.cue cannot be read")
    with caplog.at_level("WARNING", logger="app.worker.warm"):
        assert warm.warm_schemas(5) is True
    assert "the schemas could not be loaded" in " ".join(r.getMessage() for r in caplog.records)


def test_with_no_tenant_nothing_is_said(manager, caplog):
    manager.list_tenant_ids.return_value = []
    with caplog.at_level("INFO", logger="app.worker.warm"):
        assert warm.warm_schemas(5) is True
    manager.get_or_clone.assert_not_called()
    assert caplog.records == []


def test_a_timeout_of_zero_turns_the_warm_start_off(manager):
    assert warm.warm_schemas(0) is True
    manager.list_tenant_ids.assert_not_called()


def test_a_slow_clone_does_not_hold_the_process_past_the_timeout(manager, caplog):
    release = threading.Event()
    manager.get_or_clone.side_effect = lambda tenant: release.wait(10)
    started = time.monotonic()
    try:
        with caplog.at_level("WARNING", logger="app.worker.warm"):
            finished = warm.warm_schemas(0.2)
        waited = time.monotonic() - started
    finally:
        release.set()                                    # let the background clone end
    assert finished is False
    assert waited < 2, f"the process waited {waited:.1f} s for a 0.2 s limit"
    assert "still loading after" in " ".join(r.getMessage() for r in caplog.records)


def test_the_clone_goes_on_in_the_background_after_the_timeout(manager):
    release = threading.Event()
    reached_beta = threading.Event()

    def get_or_clone(tenant):
        if tenant == "alpha":
            release.wait(10)
        else:
            reached_beta.set()

    manager.get_or_clone.side_effect = get_or_clone
    assert warm.warm_schemas(0.1) is False
    release.set()
    assert reached_beta.wait(5), "the clones stopped when the process stopped waiting"


# ── The libraries, preloaded in the main process ─────────────────────────────

def test_every_listed_module_is_imported():
    imported = []
    with patch("app.worker.warm.importlib.import_module", side_effect=imported.append):
        warm.preload_libraries(("alpha", "beta.sub"))
    assert imported == ["alpha", "beta.sub"]


def test_a_module_that_is_missing_does_not_stop_the_others_and_is_not_an_error(caplog):
    def import_module(name):
        if name == "missing":
            raise ImportError("No module named 'missing'")

    with patch("app.worker.warm.importlib.import_module", side_effect=import_module),             caplog.at_level("INFO", logger="app.worker.warm"):
        took = warm.preload_libraries(("a", "missing", "b"))
    assert took >= 0
    assert "2 of 3 modules preloaded" in " ".join(r.getMessage() for r in caplog.records)


def test_a_module_whose_import_raises_something_else_is_skipped_too():
    with patch("app.worker.warm.importlib.import_module", side_effect=RuntimeError("boom")):
        assert warm.preload_libraries(("a",)) >= 0           # the worker still starts


def test_the_modules_a_first_sync_imports_are_in_the_list():
    # What the measurement found: these were imported by the first sync that used each store.
    for name in ("pymongo", "boto3", "paramiko", "hvac", "redis", "kazoo.client", "git", "app.pipeline.sinks"):
        assert name in warm.PRELOADED


def test_the_worker_preloads_unless_the_warm_start_is_off():
    with patch.object(warm, "preload_libraries") as preload, patch("app.config.settings") as settings:
        settings.SCHEMA_WARM_TIMEOUT = 10
        warm.on_worker_init()
        assert preload.call_count == 1
        settings.SCHEMA_WARM_TIMEOUT = 0
        warm.on_worker_init()
        assert preload.call_count == 1


# ── The signal ───────────────────────────────────────────────────────────────

def test_a_new_pool_process_runs_the_warm_start_once_however_often_it_is_connected():
    from celery.signals import worker_process_init

    warm.connect()
    warm.connect()
    with patch.object(warm, "warm_schemas") as warm_schemas:
        worker_process_init.send(sender=None)
    assert warm_schemas.call_count == 1


def test_the_signal_waits_as_long_as_the_setting_says():
    with patch.object(warm, "warm_schemas") as warm_schemas, patch("app.config.settings") as settings:
        settings.SCHEMA_WARM_TIMEOUT = 3.5
        warm.on_worker_process_init()
    warm_schemas.assert_called_once_with(3.5)


def test_importing_the_worker_app_connects_the_warm_start():
    # In a fresh interpreter: signals are process-wide, so another test's connect() would hide a missing one.
    import os
    import subprocess
    import sys

    program = (
        "from unittest.mock import patch\n"
        "import app.worker.celery\n"
        "from app.worker import warm\n"
        "from celery.signals import worker_init, worker_process_init\n"
        "with patch.object(warm, 'warm_schemas') as w, patch.object(warm, 'preload_libraries') as p:\n"
        "    worker_init.send(sender=None)\n"
        "    worker_process_init.send(sender=None)\n"
        "print('calls', w.call_count, p.call_count)\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    env.pop("CONFIG_PATH", None)
    out = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, env=env, timeout=120)
    assert "calls 1 1" in out.stdout, out.stdout + out.stderr
