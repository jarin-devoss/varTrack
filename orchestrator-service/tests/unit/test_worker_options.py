"""
tests/unit/test_worker_options.py

How many syncs a worker runs at once, and where that number comes from (app/worker/options.py,
app/worker/__main__.py): the command line, then the environment, then config.cue, then the default.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.worker import __main__ as worker_main
from app.worker import options


# ── The order: flag, environment, config.cue, default ─────────────────────────

def test_with_nothing_set_the_defaults_are_used():
    opts = options.resolve()
    assert opts.concurrency.value == 4 and opts.concurrency.source == "default"
    assert opts.max_tasks_per_child.value == 200 and opts.max_tasks_per_child.source == "default"
    assert opts.warnings == ()


def test_config_cue_sets_the_numbers():
    opts = options.resolve(bundle_workers={"concurrency": 8, "max_tasks_per_child": 500})
    assert (opts.concurrency.value, opts.concurrency.source) == (8, "config.cue workers.concurrency")
    assert (opts.max_tasks_per_child.value, opts.max_tasks_per_child.source) == (
        500, "config.cue workers.max_tasks_per_child")


def test_config_cue_may_name_only_one_of_the_two():
    opts = options.resolve(bundle_workers={"concurrency": 2})
    assert opts.concurrency.value == 2
    assert opts.max_tasks_per_child.source == "default"


def test_the_environment_is_over_config_cue():
    opts = options.resolve(env={"VARTRACK_WORKER_CONCURRENCY": "16"}, bundle_workers={"concurrency": 8})
    assert (opts.concurrency.value, opts.concurrency.source) == (16, "VARTRACK_WORKER_CONCURRENCY")


def test_the_flag_is_over_the_environment_and_config_cue():
    opts = options.resolve(
        flags={"concurrency": "2"},
        env={"VARTRACK_WORKER_CONCURRENCY": "16"},
        bundle_workers={"concurrency": 8},
    )
    assert (opts.concurrency.value, opts.concurrency.source) == (2, "--concurrency")


def test_each_setting_is_decided_on_its_own():
    opts = options.resolve(
        flags={"max_tasks_per_child": "50"},
        env={"VARTRACK_WORKER_CONCURRENCY": "6"},
        bundle_workers={"concurrency": 8, "max_tasks_per_child": 500},
    )
    assert opts.concurrency.value == 6
    assert (opts.max_tasks_per_child.value, opts.max_tasks_per_child.source) == (50, "--max-tasks-per-child")


def test_an_empty_variable_is_not_set():
    # docker compose passes VARTRACK_WORKER_CONCURRENCY= when the operator set nothing.
    opts = options.resolve(env={"VARTRACK_WORKER_CONCURRENCY": "", "WORKER_CONCURRENCY": "  "},
                           bundle_workers={"concurrency": 8})
    assert opts.concurrency.source == "config.cue workers.concurrency"
    assert opts.warnings == ()


def test_the_bundle_may_spell_a_key_in_camel_case():
    opts = options.resolve(bundle_workers={"maxTasksPerChild": 7})
    assert opts.max_tasks_per_child.value == 7


def test_zero_tasks_per_child_means_never_and_is_not_the_default():
    opts = options.resolve(env={"VARTRACK_WORKER_MAX_TASKS_PER_CHILD": "0"})
    assert opts.max_tasks_per_child.value == 0
    assert opts.max_tasks_per_child.source == "VARTRACK_WORKER_MAX_TASKS_PER_CHILD"


# ── The old variable is read for one release, and said ───────────────────────

def test_the_old_variable_still_sets_the_number_and_names_the_new_one():
    opts = options.resolve(env={"WORKER_CONCURRENCY": "12"})
    assert opts.concurrency.value == 12
    assert "deprecated" in opts.concurrency.source
    assert len(opts.warnings) == 1
    assert "VARTRACK_WORKER_CONCURRENCY" in opts.warnings[0]


def test_the_new_variable_is_over_the_old_one():
    opts = options.resolve(env={"WORKER_CONCURRENCY": "12", "VARTRACK_WORKER_CONCURRENCY": "3"})
    assert opts.concurrency.value == 3


def test_the_old_variable_is_over_config_cue():
    # What an operator had before config.cue could say it keeps meaning what it meant.
    opts = options.resolve(env={"WORKER_CONCURRENCY": "12"}, bundle_workers={"concurrency": 8})
    assert opts.concurrency.value == 12


# ── A value that is not usable is an error naming where it came from ─────────

@pytest.mark.parametrize("bad", ["0", "65", "1000", "-1", "four", "4.5", "0x10", "1e2", "٣"])
def test_a_concurrency_outside_one_to_sixty_four_or_not_a_whole_number_is_refused(bad):
    with pytest.raises(options.WorkerOptionError) as caught:
        options.resolve(env={"VARTRACK_WORKER_CONCURRENCY": bad})
    assert "VARTRACK_WORKER_CONCURRENCY" in str(caught.value)


def test_the_error_names_the_flag_when_the_flag_is_wrong():
    with pytest.raises(options.WorkerOptionError, match="--concurrency"):
        options.resolve(flags={"concurrency": "0"})


def test_a_wrong_value_in_config_cue_is_refused_too():
    # The loader checks the range; this process does not rely on it.
    with pytest.raises(options.WorkerOptionError, match="config.cue workers.concurrency"):
        options.resolve(bundle_workers={"concurrency": 100})


def test_only_the_winning_source_is_read():
    # As with any setting: the flag decides, and a bad variable under it is not looked at.
    opts = options.resolve(flags={"concurrency": "2"}, env={"VARTRACK_WORKER_CONCURRENCY": "four"})
    assert opts.concurrency.value == 2


def test_a_boolean_is_not_a_number_of_workers():
    with pytest.raises(options.WorkerOptionError):
        options.resolve(bundle_workers={"concurrency": True})


def test_a_negative_tasks_per_child_is_refused():
    with pytest.raises(options.WorkerOptionError, match="VARTRACK_WORKER_MAX_TASKS_PER_CHILD"):
        options.resolve(env={"VARTRACK_WORKER_MAX_TASKS_PER_CHILD": "-5"})


def test_the_description_says_where_each_value_came_from():
    text = options.resolve(env={"VARTRACK_WORKER_CONCURRENCY": "8"}).describe()
    assert text == ("worker: concurrency=8 (VARTRACK_WORKER_CONCURRENCY), "
                    "max_tasks_per_child=200 (default)")


# ── What celery is started with ───────────────────────────────────────────────

def test_celery_gets_the_queues_a_worker_has_always_served():
    argv = worker_main.celery_arguments(options.resolve(), worker_main.DEFAULT_QUEUES, "info", [])
    assert argv == ["worker", "--loglevel=info", "-Q", "webhooks,sync,schema",
                    "--concurrency=4", "--max-tasks-per-child=200"]


def test_extra_arguments_go_to_celery_unchanged():
    argv = worker_main.celery_arguments(options.resolve(), "sync", "debug", ["--hostname", "w1@%h", "-E"])
    assert argv[-3:] == ["--hostname", "w1@%h", "-E"]
    assert argv[:3] == ["worker", "--loglevel=debug", "-Q"]


def test_never_replacing_a_process_leaves_the_flag_out():
    opts = options.resolve(env={"VARTRACK_WORKER_MAX_TASKS_PER_CHILD": "0"})
    argv = worker_main.celery_arguments(opts, worker_main.DEFAULT_QUEUES, "info", [])
    assert not any(a.startswith("--max-tasks-per-child") for a in argv)


# ── The command ───────────────────────────────────────────────────────────────

@pytest.fixture
def fake_celery():
    """app.worker.celery stands in for the real app, so the command can run without a broker."""
    app = MagicMock()
    app.worker_main.return_value = None
    module = MagicMock(celery=app)
    with patch.dict(sys.modules, {"app.worker.celery": module}):
        yield app


def _run(argv, env, workers=None):
    """worker_main.main(argv) with *env* as the whole environment and config.cue's workers block."""
    with patch.dict(os.environ, env, clear=True), patch("app.config.settings") as settings:
        settings.WORKERS = workers or {}
        return worker_main.main(argv)


def test_the_command_starts_celery_with_what_config_cue_says(fake_celery):
    code = _run([], {}, workers={"concurrency": 8, "max_tasks_per_child": 300})
    assert code == 0
    (argv,), _ = fake_celery.worker_main.call_args
    assert "--concurrency=8" in argv and "--max-tasks-per-child=300" in argv


def test_the_environment_beats_config_cue_in_the_command(fake_celery):
    _run([], {"VARTRACK_WORKER_CONCURRENCY": "2"}, workers={"concurrency": 8})
    (argv,), _ = fake_celery.worker_main.call_args
    assert "--concurrency=2" in argv


def test_the_flag_beats_the_environment_in_the_command(fake_celery):
    _run(["--concurrency", "5"], {"VARTRACK_WORKER_CONCURRENCY": "2"}, workers={"concurrency": 8})
    (argv,), _ = fake_celery.worker_main.call_args
    assert "--concurrency=5" in argv


def test_the_command_passes_unknown_arguments_on(fake_celery):
    _run(["--hostname", "w1@%h"], {})
    (argv,), _ = fake_celery.worker_main.call_args
    assert argv[-2:] == ["--hostname", "w1@%h"]


def test_a_bad_value_stops_the_worker_before_celery_starts(fake_celery, capsys):
    code = _run([], {"VARTRACK_WORKER_CONCURRENCY": "four"})
    assert code == 2
    assert "VARTRACK_WORKER_CONCURRENCY" in capsys.readouterr().err
    fake_celery.worker_main.assert_not_called()


def test_never_replacing_a_process_clears_the_apps_own_default(fake_celery):
    _run([], {"VARTRACK_WORKER_MAX_TASKS_PER_CHILD": "0"})
    assert fake_celery.conf.worker_max_tasks_per_child is None


def test_the_command_says_where_each_value_came_from(fake_celery, caplog):
    with caplog.at_level("INFO", logger="app.worker"):
        _run([], {"WORKER_CONCURRENCY": "3"}, workers={"max_tasks_per_child": 9})
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "concurrency=3 (WORKER_CONCURRENCY (deprecated))" in said
    assert "max_tasks_per_child=9 (config.cue workers.max_tasks_per_child)" in said
    assert "VARTRACK_WORKER_CONCURRENCY" in said   # the warning names the new variable


# ── config.cue's workers block reaches the settings ───────────────────────────

def test_the_bundles_workers_block_becomes_the_settings_workers():
    from app.config import _bundle_overrides

    assert _bundle_overrides({"workers": {"concurrency": 8}})["WORKERS"] == {"concurrency": 8}


def test_a_bundle_without_a_workers_block_sets_nothing():
    from app.config import _bundle_overrides

    assert "WORKERS" not in _bundle_overrides({})
    assert "WORKERS" not in _bundle_overrides({"workers": {}})
