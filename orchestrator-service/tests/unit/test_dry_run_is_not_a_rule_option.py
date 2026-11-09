"""
A dry run is something a person starts (the UI's Dry run, `vt sync --dry-run`, the dry-run webhook), never a setting
of a rule. A file that still sets `dry_run` or `prune.dry_run` loads, and says what to do instead.
"""
from __future__ import annotations

import logging

from app.pipeline.renamed_options import with_current_names
from app.tasks.payload import extract_prune_config


def test_a_rule_that_sets_dry_run_loses_it_and_the_warning_says_what_to_do_instead(caplog):
    caplog.set_level(logging.WARNING)
    rule = with_current_names({"datasource": "mongo", "dry_run": True, "file_name": "a.yaml"}, "bundle.json of acme")
    assert rule == {"datasource": "mongo", "file_name": "a.yaml"}
    assert "dry_run was removed and has no effect" in caplog.text
    assert "vt sync --dry-run" in caplog.text and "writes on every push" in caplog.text


def test_prune_dry_run_goes_and_the_rest_of_the_block_stays(caplog):
    caplog.set_level(logging.WARNING)
    rule = with_current_names({"prune": {"enabled": True, "dry_run": True, "protection": ["keep.*"]}}, "config.cue")
    assert rule == {"prune": {"enabled": True, "protection": ["keep.*"]}}
    assert "prune.dry_run was removed" in caplog.text and "a dry run reports what a prune would delete" in caplog.text


def test_a_prune_block_with_only_dry_run_is_a_disabled_block():
    rule = with_current_names({"prune": {"dry_run": True}}, "vartrack.json")
    assert rule == {"prune": {}}
    assert extract_prune_config(rule).enabled is False


def test_the_former_flat_dry_run_prune_is_removed_too(caplog):
    caplog.set_level(logging.WARNING)
    assert with_current_names({"prune": True, "dry_run_prune": True}, "bundle.json") == {"prune": True}
    assert "dry_run_prune was removed" in caplog.text


def test_a_rule_without_them_is_returned_untouched():
    rule = {"prune": {"enabled": True}, "file_name": "a.yaml"}
    assert with_current_names(rule) is rule
