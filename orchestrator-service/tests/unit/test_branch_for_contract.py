"""One answer to "which branch feeds this environment".

The gateway's permission engine (gateway-service/internal/authz, `branchFor`), the ETL's environment resolver
(`env_resolver.resolve`) and self-heal (`env_resolver.heal_branches`) used to answer it three ways, so a Sync pressed
in the web UI failed for `default` while the heal of the same environment worked. The cases live in one table the
engine's Go test also reads: `gateway-service/internal/authz/testdata/branch_for.json`.

Here the table is held against the other two: the branch the engine judges is a branch whose files the ETL puts in
that environment, an environment the engine says nothing feeds is one the rule's own branch does not reach, and a
branch self-heal reads is a branch the engine knows.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.pipeline.env_resolver import heal_branches, resolve

TABLE = pathlib.Path(__file__).resolve().parents[3] / "gateway-service" / "internal" / "authz" / "testdata" / "branch_for.json"
ROWS = json.loads(TABLE.read_text(encoding="utf-8"))


def _rule(row: dict) -> dict:
    spec = row["rule"]
    return {
        "branch_map": spec.get("branch_map") or {},
        "branch_as_env": bool(spec.get("branch_as_env")),
        "branch": spec.get("branch"),
        # file_path_map is {env: path}; the table names the path of the row's own environment
        "file_path_map": {env: (row["file_path"] if env == row["env"] else f"{env}/app.yaml")
                          for env in spec.get("file_path_envs") or []},
    }


def _resolve(rule: dict, branch: str, file_path: str | None) -> str | None:
    return resolve(branch=branch, file_path=file_path, branch_map=rule["branch_map"],
                   file_path_map=rule["file_path_map"], branch_as_env=rule["branch_as_env"])


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_the_branch_the_engine_judges_lands_its_files_in_that_environment(row):
    rule = _rule(row)
    if row["branch"]:
        assert _resolve(rule, row["branch"], row.get("file_path")) == row["env"]


@pytest.mark.parametrize("row", [r for r in ROWS if not r["branch"]], ids=lambda r: r["name"])
def test_an_environment_nothing_feeds_is_one_the_rules_own_branch_does_not_reach(row):
    rule = _rule(row)
    own = rule["branch"] or "main"
    assert _resolve(rule, own, row.get("file_path")) != row["env"]


@pytest.mark.parametrize("row", [r for r in ROWS if r["branch"] and not r["rule"].get("branch_as_env")],
                         ids=lambda r: r["name"])
def test_the_branch_the_engine_judges_is_one_self_heal_reads(row):
    """Self-heal re-syncs the branches the rule names, else its own: the branch a Sync in the UI reads is one of them."""
    assert row["branch"] in heal_branches(_rule(row))


def test_a_rule_with_no_branch_is_healed_on_main():
    assert heal_branches({}) == ["main"]
    assert heal_branches({"branch": None}) == ["main"]
    assert heal_branches({"branch": "release"}) == ["release"]
    assert heal_branches({"branch": "release", "branch_map": {"a": "x", "b": "y"}}) == ["a", "b"]


def test_the_table_covers_every_way_an_environment_is_fed():
    """A new strategy in the resolver needs a row here, or this fails."""
    kinds = {
        "branch_map": any(r["rule"].get("branch_map") and r["branch"] for r in ROWS),
        "branch_as_env": any(r["rule"].get("branch_as_env") and r["branch"] for r in ROWS),
        "file_path_map": any(r["rule"].get("file_path_envs") and r["branch"] for r in ROWS),
        "nothing": any(not r["branch"] for r in ROWS),
    }
    assert all(kinds.values()), kinds


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_the_open_web_ui_answers_as_the_engine_does(row):
    """With sign-in off the orchestrator answers for itself (repo_scope.branch_for); it must give the engine's branch."""
    from app.pipeline import repo_scope

    assert repo_scope.branch_for(_rule(row), row["env"]) == row["branch"]


def test_the_open_viewer_refuses_an_environment_nothing_feeds_as_the_engine_does():
    from app.ui import access

    rule = {"platform": "gitea", "datasource": "redis", "branch_map": {"develop": "staging"}}
    fed = access.Viewer().decide(rule, "sync", "staging")
    assert fed.allowed and fed.branch == "develop"
    unfed = access.Viewer().decide(rule, "sync", "qa")
    assert not unfed.allowed and unfed.denied_by == "rule" and unfed.branch == ""
    assert unfed.reason.startswith("permission denied: nothing feeds qa in the redis rule")
    # there is no environment called default: it is fed by a branch only when the rule's branch_map says so
    assert access.Viewer().decide({"datasource": "plain", "branch_map": {"main": "default"}}, "dry_run", "default").branch == "main"
    assert not access.Viewer().decide({"datasource": "plain", "branch_map": {"main": "production"}}, "dry_run", "default").allowed
