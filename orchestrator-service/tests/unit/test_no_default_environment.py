"""
There is no "default" environment.

A push nothing names feeds no environment of the rule: it is skipped and said so, never written to an environment nobody
declared. A rule that names no environments is refused when it is loaded (tests/unit/test_rule_layers.py), and the web UI
lists it as refused, with why. An environment nothing feeds is a leftover: its keys are not compared with Git.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.routers import ui
from app.pipeline import rule_layers, stage_etl
from app.pipeline.models import ETLResult, PayloadContext
from app.tasks import etl
from app.ui import access, ledger, views


def _ctx(branch="main", **rule):
    return PayloadContext(platform="gitea", repo_url="http://gitea/acme/configs.git", ref=f"refs/heads/{branch}", branch=branch,
                          commit_sha="abc", tag=None, pr_number=None,
                          rule_config={"platform": "gitea", "datasource": "redis", "file_name": "app.yaml", **rule},
                          parsed_payload={})


def _etl(ctx):
    schema = MagicMock()
    schema.current_schema_root.return_value = MagicMock(exists=lambda: False)
    with (
        patch.object(stage_etl, "collect_files", return_value=[("app.yaml", "a: 1\n")]),
        patch("app.schema_registry.manager.get_schema_manager", return_value=schema),
    ):
        return stage_etl.run_stage_etl(ctx, "redis", "acme")


class TestAPushNothingNamesIsSkipped:
    def test_a_branch_the_branch_map_does_not_name_feeds_nothing(self):
        result = _etl(_ctx("feature/x", branch_map={"main": "production", "develop": "staging"}))
        assert result.files == [] and result.errors == []
        assert result.unfed == {"app.yaml": "branch feature/x feeds no environment of gitea/redis"}

    def test_a_branch_the_branch_map_names_is_synced(self):
        result = _etl(_ctx("develop", branch_map={"main": "production", "develop": "staging"}))
        assert [(f.env, f.flat_data) for f in result.files] == [("staging", {"a": "1"})]
        assert result.unfed == {}

    def test_there_is_no_fallback_environment_for_a_rule_that_names_none(self):
        # such a rule is refused when it is loaded; if one reaches the ETL it is not written to a made-up environment
        result = _etl(_ctx("main"))
        assert result.files == [] and list(result.unfed) == ["app.yaml"]

    def test_branch_as_env_still_takes_every_branch(self):
        assert [f.env for f in _etl(_ctx("feature/x", branch_as_env=True)).files] == ["feature/x"]

    def test_a_task_whose_every_file_feeds_nothing_ends_nothing_to_do_and_says_why(self):
        unfed = ETLResult(unfed={"app.yaml": "branch feature/x feeds no environment of gitea/redis"})
        ctx = _ctx("feature/x")
        recorder = MagicMock()
        with (
            patch.object(etl, "run_stage_payload", return_value=ctx),
            patch("app.pipeline.repo_overrides.apply_repo_overrides", side_effect=lambda c, t=None: c),
            patch.object(etl, "run_stage_etl", return_value=unfed),
            patch.object(etl, "run_stage_sync") as sync,
            patch.object(etl, "ui_recorder", recorder),
            patch.object(etl.process_webhook_task, "update_state"),
            patch("app.monitoring.tracer.start_webhook_span"),
        ):
            out = etl.process_webhook_task.run(platform="gitea", datasource="redis", raw_payload="{}", headers={},
                                               received_at=0.0, rule_config={}, tenant_id="acme", dry_run=False)
        assert out["state"] == "SUCCEEDED" and out["message"] == "nothing_to_do"
        sync.assert_not_called()
        message = recorder.task_finished.call_args.kwargs["message"]
        assert message == "nothing to do: branch feature/x feeds no environment of gitea/redis"


RULE = {"platform": "gitea", "datasource": "redis", "branch_map": {"develop": "staging"}, "repositories": ["acme/configs"]}


class TestALeftoversKeysAreNotComparedWithGit:
    def test_the_keys_of_an_environment_nothing_feeds_answer_409_with_the_reason(self, monkeypatch):
        monkeypatch.setattr(ui, "_resolve_rule", lambda *a, **k: ("acme", RULE, access.FULL_ACCESS, []))
        monkeypatch.setattr(ledger, "get_target", lambda tenant, ds, env: {"env": env, "datasource": "redis"})
        with pytest.raises(HTTPException) as caught:
            ui.list_keys(None, "gitea", "redis", "main", None, access.Viewer())
        assert caught.value.status_code == 409
        assert caught.value.detail.startswith("nothing feeds main in the redis rule")

    def test_the_keys_of_an_environment_a_branch_feeds_are_compared(self, monkeypatch):
        monkeypatch.setattr(ui, "_resolve_rule", lambda *a, **k: ("acme", RULE, access.FULL_ACCESS, []))
        monkeypatch.setattr(ledger, "get_target", lambda tenant, ds, env: {"env": env, "datasource": "redis"})
        monkeypatch.setattr(views, "key_entries", lambda rule, tenant, record: [{"key": "a"}])
        assert ui.list_keys(None, "gitea", "redis", "staging", None, access.Viewer()) == {"keys": [{"key": "a"}]}


@pytest.fixture
def refused_rule(monkeypatch):
    rule = {"platform": "gitea", "datasource": "zookeeper", "file_name": "app.yaml", "repositories": ["acme/configs"]}
    why = rule_layers.refusal(rule)
    rule_layers._refused.clear()
    rule_layers._refused["acme"] = {("gitea", "zookeeper"): (rule, why)}
    monkeypatch.setattr(ui, "_rules", lambda tenant: [])
    monkeypatch.setattr(ui, "_tenants", lambda request, tenant: ["acme"])
    monkeypatch.setattr(ledger, "list_targets", lambda tenant: [])
    yield rule, why
    rule_layers._refused.clear()


class TestARefusedRuleIsListedWithWhy:
    def test_the_rules_list_has_it_as_refused_with_no_environments(self, refused_rule):
        _, why = refused_rule
        [view] = ui._refused_views(access.Viewer(), ["acme"])
        assert view["name"] == "gitea/zookeeper" and view["tenant"] == "acme"
        assert view["refused"] == why and view["targets"] == [] and view["canSync"] is False

    def test_the_rules_own_page_shows_it_as_refused(self, refused_rule):
        _, why = refused_rule
        view = ui.get_rule(None, "gitea", "zookeeper", None, False, access.Viewer())
        assert view["refused"] == why and view["targets"] == []

    def test_a_sync_or_dry_run_of_it_answers_409_with_the_reason(self, refused_rule):
        _, why = refused_rule
        body = ui.SyncRequest(envs=["main"], dryRun=True)
        with pytest.raises(HTTPException) as caught:
            ui.sync_rule(None, "gitea", "zookeeper", body, None, access.Viewer())
        assert caught.value.status_code == 409 and caught.value.detail == why

    def test_someone_who_may_not_read_it_is_told_it_does_not_exist(self, refused_rule, monkeypatch):
        class Nobody(access.Viewer):
            def rule_access(self, rule, records=(), tenant=""):
                return access.Access()

            def visible_rules(self, rules, records, tenant=""):
                return []

        assert ui._refused_views(Nobody(), ["acme"]) == []
        with pytest.raises(HTTPException) as caught:
            ui.get_rule(None, "gitea", "zookeeper", None, False, Nobody())
        assert caught.value.status_code == 404

    def test_a_rule_that_is_not_refused_has_no_refused_field(self):
        assert "refused" not in views.rule_view(dict(RULE), "acme", [], [])


class TestAnEnvironmentTheCliWroteLastIsSyncedFromTheRulesRepository:
    """`vt sync --file` records its repository as "local": no Git behind it. A sync from the web UI must not try to read that."""

    RULE = {"repo_url": "http://gitea:3000/acme/configs.git"}

    def test_the_repository_an_environment_was_synced_from_is_read(self):
        record = {"repoUrl": "http://gitea:3000/acme/other.git"}
        assert ui._git_url(record, self.RULE) == "http://gitea:3000/acme/other.git"
        assert ui._git_url({"repoUrl": "git@gitea:acme/other.git"}, self.RULE) == "git@gitea:acme/other.git"

    def test_what_the_cli_wrote_is_read_from_the_rules_repository(self):
        assert ui._git_url({"repoUrl": "local"}, self.RULE) == "http://gitea:3000/acme/configs.git"

    def test_an_environment_with_no_record_is_read_from_the_rules_repository(self):
        assert ui._git_url(None, self.RULE) == "http://gitea:3000/acme/configs.git"
        assert ui._git_url({}, self.RULE) == "http://gitea:3000/acme/configs.git"
