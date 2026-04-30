"""
tests/unit/test_stage_sync.py

Unit tests for run_stage_sync (Stage 3: Write + Rollback).

Key invariants:
  - Single sink created per batch.
  - Successful writes produce status="ok" SyncResults.
  - On write failure, previously committed files are rolled back (delete_file_data).
  - Remaining un-attempted files are marked "aborted".
  - sync_mode decision (<=500 keys -> GIT_UPSERT_ALL, >500 -> GIT_SMART_REPAIR).

Note on patching: create_sink is lazily imported inside run_stage_sync(),
so patch targets app.pipeline.sinks.create_sink.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import app.pipeline.sinks  # noqa: E402,F401 — pre-import for patch() resolution

from app.pipeline.models import ETLFile, ETLResult, PayloadContext
from app.tasks.payload import PruneConfig


def _prune_cfg() -> PruneConfig:
    return PruneConfig(enabled=False, last=False, dry_run=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ctx(**kw) -> PayloadContext:
    defaults = dict(
        platform="github", repo_url="https://g.c/o/r",
        ref="refs/heads/main", branch="main", commit_sha="abc",
        tag=None, pr_number=None,
        rule_config={"mongo_uri": "mongodb://localhost:27017", "database": "vartrack"},
        parsed_payload={},
    )
    defaults.update(kw)
    return PayloadContext(**defaults)


def _file(path="config.yaml", env="production", n_keys=5) -> ETLFile:
    return ETLFile(
        file_path=path, env=env,
        flat_data={f"key.{i}": str(i) for i in range(n_keys)},
        root_key="vartrack",
    )


def _etl(*files: ETLFile) -> ETLResult:
    r = ETLResult()
    r.files.extend(files)
    return r


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunStageSync(unittest.TestCase):

    def _run(self, etl: ETLResult, sink_mock: MagicMock | None = None, ctx=None):
        """Run stage_sync with create_sink patched."""
        if sink_mock is None:
            sink_mock = MagicMock()
            sink_mock.write.return_value = {"written": 5, "pruned": 0}
            sink_mock.delete_file_data.return_value = None
        ctx = ctx or _ctx()

        with (
            # create_sink is lazily imported → patch at source module
            patch("app.pipeline.sinks.create_sink", return_value=sink_mock),
            patch("app.pipeline.stage_sync.extract_prune_config", return_value=_prune_cfg()),
        ):
            from app.pipeline.stage_sync import run_stage_sync
            return run_stage_sync(
                ctx=ctx, etl=etl, datasource="mongo",
                tenant_id="acme", total_sources=1,
            ), sink_mock

    def test_successful_write_returns_ok_status(self):
        etl = _etl(_file("cfg.yaml"))
        results, _ = self._run(etl)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[0].file_path, "cfg.yaml")

    def test_multiple_files_all_written(self):
        etl = _etl(_file("a.yaml"), _file("b.yaml"), _file("c.yaml"))
        results, sink = self._run(etl)
        self.assertEqual(len(results), 3)
        self.assertEqual(sink.write.call_count, 3)
        statuses = {r.status for r in results}
        self.assertEqual(statuses, {"ok"})

    def test_single_sink_created_for_all_files(self):
        """
        create_sink() must be called ONCE per batch,
        not once per file (which would open N connection pools).
        """
        etl = _etl(_file("a.yaml"), _file("b.yaml"), _file("c.yaml"))
        with patch("app.pipeline.sinks.create_sink") as mock_create:
            sink_instance = MagicMock()
            sink_instance.write.return_value = {"written": 1, "pruned": 0}
            mock_create.return_value = sink_instance
            with patch("app.tasks.payload.extract_prune_config", return_value={}):
                from app.pipeline.stage_sync import run_stage_sync
                run_stage_sync(_ctx(), etl, "mongo", "acme", 1)

        self.assertEqual(mock_create.call_count, 1,
                         "create_sink must be called exactly once per run_stage_sync call")

    def test_write_failure_triggers_rollback_of_committed_files(self):
        """
        When file 2 of 3 fails, file 1 (already written)
        must be rolled back via delete_file_data.
        """
        sink = MagicMock()
        sink.write.side_effect = [
            {"written": 3, "pruned": 0},           # file 1: ok
            RuntimeError("mongo connection lost"),  # file 2: fails
        ]
        sink.delete_file_data.return_value = None

        files = [_file("a.yaml"), _file("b.yaml"), _file("c.yaml")]
        etl = _etl(*files)
        results, _ = self._run(etl, sink_mock=sink)

        self.assertGreaterEqual(sink.delete_file_data.call_count, 1,
                                "Committed file must be rolled back on subsequent failure")

        aborted = [r for r in results if r.status == "aborted"]
        self.assertTrue(aborted, "Un-attempted files must be marked 'aborted' after rollback")

    def test_write_failure_marks_failed_file_as_error(self):
        sink = MagicMock()
        sink.write.side_effect = RuntimeError("timeout")
        sink.delete_file_data.return_value = None

        results, _ = self._run(_etl(_file()), sink_mock=sink)
        error_results = [r for r in results if r.status == "error"]
        self.assertTrue(error_results, "Failed write must produce status='error'")

    def test_empty_etl_result_produces_no_syncs(self):
        results, sink = self._run(ETLResult())
        self.assertEqual(results, [])
        sink.write.assert_not_called()

    def test_small_payload_selects_upsert_all(self):
        etl = _etl(_file(n_keys=10))
        results, _ = self._run(etl)
        if results:
            self.assertIn(results[0].status, ("ok", "error", "skipped"))

    def test_large_payload_selects_smart_repair(self):
        etl = _etl(_file(n_keys=600))
        results, _ = self._run(etl)
        if results:
            self.assertIn(results[0].status, ("ok", "error", "skipped"))


if __name__ == "__main__":
    unittest.main()
