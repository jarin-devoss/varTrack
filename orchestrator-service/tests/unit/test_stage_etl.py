"""
tests/unit/test_stage_etl.py

Unit tests for run_stage_etl (Stage 2: Extract → Transform → Validate).

Note on patching: stage_etl.py uses lazy (in-function) imports for
get_schema_manager and Validator, so patches must target the source modules
rather than the stage_etl namespace.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.pipeline.models import ETLResult, PayloadContext


def _ctx(**kw) -> PayloadContext:
    defaults = dict(
        platform="github", repo_url="https://github.com/org/repo",
        ref="refs/heads/main", branch="main", commit_sha="abc",
        tag=None, pr_number=None,
        rule_config={"branch_map": {"main": "production"}, "variables_map": {}},
        parsed_payload={},
    )
    defaults.update(kw)
    return PayloadContext(**defaults)


class TestRunStageETL(unittest.TestCase):

    def _run(
        self,
        files: list,
        flat_data=None,
        validate_raises=None,
        rule_config=None,
    ) -> ETLResult:
        flat_data = flat_data or {"db.host": "localhost", "db.port": "5432"}
        ctx = _ctx(rule_config=rule_config or {"variables_map": {}, "branch_map": {}})

        mock_schema = MagicMock()
        mock_schema.tenant_schema_root.return_value = Path("/nonexistent/schemas")

        mock_validator = MagicMock()
        mock_validator._find_schema.return_value = None
        if validate_raises:
            mock_validator.validate.side_effect = validate_raises
        else:
            mock_validator.validate.return_value = None

        with (
            patch("app.pipeline.stage_etl.collect_files", return_value=files),
            patch("app.schema_registry.manager.get_schema_manager",
                  return_value=mock_schema),
            patch("app.pipeline.validator.Validator", return_value=mock_validator),
            patch("app.pipeline.env_resolver.resolve", return_value="production"),
            patch("app.pipeline.transformer.transform", return_value=flat_data),
        ):
            from app.pipeline.stage_etl import run_stage_etl
            return run_stage_etl(ctx, datasource="mongo", tenant_id="acme")

    def test_single_file_produces_one_etlfile(self):
        result = self._run([("config.yaml", "db:\n  host: localhost\n")])
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].env, "production")
        self.assertEqual(result.files[0].file_path, "config.yaml")

    def test_multiple_files_all_processed(self):
        files = [
            ("config.yaml", "db:\n  host: localhost\n"),
            ("secrets.yaml", "token: abc123\n"),
        ]
        result = self._run(files)
        self.assertEqual(len(result.files), 2)
        paths = {f.file_path for f in result.files}
        self.assertIn("config.yaml", paths)
        self.assertIn("secrets.yaml", paths)

    def test_empty_file_list_returns_empty_result(self):
        result = self._run([])
        self.assertEqual(result.files, [])
        self.assertEqual(result.errors, [])

    def test_flat_data_stored_on_etlfile(self):
        flat = {"key.a": "1", "key.b": "2"}
        result = self._run([("cfg.json", '{"key": {"a": 1, "b": 2}}')], flat_data=flat)
        self.assertEqual(result.files[0].flat_data, flat)

    def test_cue_validation_failure_strict_skips_file(self):
        """
        strict=True CUE failure adds file to skipped
        (not to result.files). The file is never written.
        """
        from app.pipeline.validator import CUEValidationError

        result = self._run(
            [("config.yaml", "x: 1")],
            validate_raises=CUEValidationError("field missing"),
            rule_config={"strict_validation": True, "variables_map": {}, "branch_map": {}},
        )
        self.assertIn("config.yaml", result.skipped,
                      "Strict CUE failure must add file to result.skipped")
        self.assertEqual(len(result.files), 0,
                         "File with strict CUE failure must not be in result.files")

    def test_unexpected_exception_goes_to_errors_not_skipped(self):
        """
        Unexpected exceptions during ETL must go to
        ETLResult.errors, not silently swallowed.
        """
        with (
            patch("app.pipeline.stage_etl.collect_files",
                  return_value=[("bad.yaml", "content")]),
            patch("app.schema_registry.manager.get_schema_manager",
                  return_value=MagicMock(
                      tenant_schema_root=MagicMock(return_value=Path("/x")))),
            patch("app.pipeline.validator.Validator", return_value=MagicMock()),
            patch("app.pipeline.env_resolver.resolve", return_value="production"),
            patch("app.pipeline.transformer.transform",
                  side_effect=RuntimeError("unexpected internal error")),
        ):
            from app.pipeline.stage_etl import run_stage_etl
            result = run_stage_etl(_ctx(), datasource="mongo", tenant_id="acme")

        self.assertTrue(result.errors, "Unexpected exception must appear in ETLResult.errors")
        self.assertFalse(result.files)

    def test_validation_ok_sets_etlfile_validation_ok(self):
        result = self._run([("cfg.yaml", "x: 1")])
        self.assertEqual(result.files[0].validation, "ok")


if __name__ == "__main__":
    unittest.main()
