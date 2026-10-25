"""The old option names (env_as_branch, env_as_pr, env_as_tags, env_as_prefix) still work."""
import json
from types import SimpleNamespace

import pytest

from app.pipeline import renamed_options
from app.pipeline.renamed_options import upgrade_bundle, with_current_names


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(renamed_options, "_warned", set())


def test_old_names_move_to_the_current_ones():
    rule = {"datasource": "mongo", "env_as_branch": True, "env_as_pr": True, "env_as_tags": False}
    assert with_current_names(rule) == {"datasource": "mongo", "branch_as_env": True, "pr_as_env": True, "tag_as_env": False}
    assert "env_as_pr" in rule  # the caller's dict is left alone


def test_the_current_name_wins_when_both_are_set():
    assert with_current_names({"env_as_pr": True, "pr_as_env": False}) == {"pr_as_env": False}


def test_a_rule_without_old_names_is_returned_as_is():
    rule = {"pr_as_env": True}
    assert with_current_names(rule) is rule
    assert with_current_names({}) == {} and with_current_names(None) is None


def test_each_source_warns_once(caplog):
    for _ in range(3):
        with_current_names({"env_as_pr": True}, "bundle.json")
    assert [r.getMessage() for r in caplog.records] == ["bundle.json: env_as_pr is now pr_as_env; the old name still works"]


def test_a_cue_bundle_upgrades_rules_and_s3_datasources():
    bundle = upgrade_bundle({
        "rules": [{"datasource": "s3", "env_as_branch": True}],
        "datasources": [{"s3": {"bucket": "b", "env_as_prefix": True}}, {"redis": {"env_as_prefix": True}}],
    })
    assert bundle["rules"] == [{"datasource": "s3", "branch_as_env": True}]
    assert bundle["datasources"][0]["s3"] == {"bucket": "b", "prefix_as_env": True}
    assert bundle["datasources"][1]["redis"] == {"env_as_prefix": True}  # not an S3 option elsewhere


def test_bundle_json_with_old_names_loads_with_the_current_ones(tmp_path):
    from app.schema_registry.manager import _load_bundle

    (tmp_path / "bundle.json").write_text(json.dumps([{"platform": "gitea", "datasource": "mongo", "env_as_pr": True}]))
    assert _load_bundle(tmp_path) == [{"platform": "gitea", "datasource": "mongo", "pr_as_env": True}]


def test_vartrack_json_with_old_names_still_overrides(monkeypatch):
    from app.pipeline import git_extractor, repo_overrides

    monkeypatch.setattr(git_extractor.GitExtractor, "extract_file",
                        lambda self, *a, **kw: json.dumps({"env_as_tags": True, "branch_as_env": False}))
    ctx = SimpleNamespace(rule_config={"datasource": "mongo", "branch_as_env": True}, repo_url="r", ref="main", commit_sha="")
    monkeypatch.setattr(repo_overrides, "replace", lambda c, **kw: SimpleNamespace(**{**vars(c), **kw}))
    out = repo_overrides.apply_repo_overrides(ctx)
    assert out.rule_config == {"datasource": "mongo", "branch_as_env": False, "tag_as_env": True}
