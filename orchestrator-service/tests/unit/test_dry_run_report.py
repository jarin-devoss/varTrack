"""
A dry run says what a sync would change, and a run started by hand on a rule with no file says so.

The report is the dry-run page's: per file, the keys a sync would add or change in the sink as it is now; the sync
panel's preview skips that (it only wants what would be deleted). A sync or dry run started from the UI re-reads the
rule's file, so a rule with none has nothing to read: that is a failed task with the reason, not a success that wrote
nothing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pipeline.models import ETLFile, ETLResult
from app.tasks import etl


def _ctx(rule=None):
    return MagicMock(repo_url="http://gitea/acme/configs.git", ref="refs/heads/main", branch="main",
                     commit_sha="abc", platform="gitea", rule_config=rule or {}, parsed_payload={})


def _etl(*files):
    result = ETLResult()
    result.files.extend(files)
    return result


def _file(env="production", validation="ok", **data):
    return ETLFile(file_path="app.yaml", env=env, flat_data=data or {"a": "1"}, root_key="", validation=validation)


class TestTheReportSaysWhatASyncWouldChange:
    def _report(self, *files, changes=True, change=None):
        with patch("app.pipeline.stage_sync.prune_candidates", return_value={}), \
                patch("app.ui.views.would_change", return_value=change or {"added": ["a"], "addedCount": 1}) as compared:
            report = etl._build_dry_run_report("t1", _ctx({"datasource": "redis"}), _etl(*files), 0.0, "redis", "demo", changes=changes)
        return report, compared

    def test_each_file_carries_what_a_sync_would_add_or_change(self):
        report, compared = self._report(_file())
        assert report["files"][0]["would_change"] == {"added": ["a"], "addedCount": 1}
        env, desired = compared.call_args.args[2], compared.call_args.args[3]
        assert (env, desired) == ("production", {"a": "1"})

    def test_the_preview_does_not_wait_for_the_sink(self):
        report, compared = self._report(_file(), changes=False)
        assert "would_change" not in report["files"][0]
        compared.assert_not_called()

    def test_a_file_that_failed_its_schema_has_nothing_to_compare(self):
        report, compared = self._report(_file(validation="failed"))
        assert "would_change" not in report["files"][0]
        compared.assert_not_called()

    def test_a_sink_that_fails_while_being_read_does_not_fail_the_dry_run(self):
        with patch("app.pipeline.stage_sync.prune_candidates", return_value={}), \
                patch("app.ui.views.would_change", side_effect=RuntimeError("boom")):
            report = etl._build_dry_run_report("t1", _ctx(), _etl(_file()), 0.0, "redis", "demo")
        assert report["state"] == "SUCCEEDED"
        assert report["files"][0]["would_change"] == {"error": "read failed: boom"}

    def test_the_schema_warning_is_in_the_report(self):
        f = _file(validation="warn")
        f.error = "port: out of range"
        report, _ = self._report(f)
        assert report["files"][0]["validation_error"] == "port: out of range"
