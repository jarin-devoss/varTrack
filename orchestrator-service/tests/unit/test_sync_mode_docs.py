"""The sentence the docs show for a sync_mode that is not a mode is the one the code says."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.enums.sync_mode import sync_mode_problem

DOCS = Path(__file__).resolve().parents[3] / "docs"

pytestmark = pytest.mark.skipif(not DOCS.is_dir(), reason="the docs are not next to the orchestrator in this checkout")


def _page(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_the_troubleshooting_page_shows_the_sentence_a_refused_rule_answers_with():
    assert f"`<platform>/<datasource>: {sync_mode_problem('<value>')}`" in _page("operations/troubleshooting.md")


def test_the_troubleshooting_page_shows_the_sentence_a_failed_sync_answers_with():
    tail = sync_mode_problem("…").split(" is not a mode", 1)[1]
    assert f'sync_mode "…" is not a mode{tail}' in _page("operations/troubleshooting.md")


def test_the_per_repo_page_shows_the_sentence_a_failed_sync_answers_with():
    assert f"vartrack.json in https://...: {sync_mode_problem('FAST')}" in _page("configuration/per-repo-overrides.md")
