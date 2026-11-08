"""
tests/unit/test_validator.py

Unit tests for the CUE schema validator (app/pipeline/validator.py).

Strategy:
  - No real CUE binary required — subprocess.run is patched.
  - Table-driven tests for: schema found, schema missing, CUE pass, CUE fail,
    strict vs warn mode, timeout.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.pipeline.validator import CUEUnavailable, CUEValidationError, Validator


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cue_ok() -> MagicMock:
    """Stub subprocess.run result: cue vet passes."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _cue_fail(message: str = "field 'db.host' is required") -> MagicMock:
    """Stub subprocess.run result: cue vet fails."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = message
    return result


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestValidatorNoSchema(unittest.TestCase):

    def test_no_schema_dir_skips_validation(self):
        """When schema_dir is None, validation is silently skipped."""
        v = Validator(schema_dir=None)
        # Must not raise
        v.validate({"key": "value"}, "config.yaml", strict=True)

    def test_nonexistent_schema_dir_skips_validation(self):
        v = Validator(schema_dir=Path("/nonexistent/dir"))
        v.validate({"key": "value"}, "config.yaml", strict=True)


class TestValidatorCUEPass(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        schema_dir = Path(self.tmpdir.name)
        # Create a fake .cue schema file
        (schema_dir / "config.yaml.cue").write_text('{ "host": string }')
        self.validator = Validator(schema_dir=schema_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_valid_data_does_not_raise(self):
        with patch("subprocess.run", return_value=_cue_ok()):
            self.validator.validate({"host": "localhost"}, "config.yaml", strict=True)

    def test_valid_data_warn_mode_does_not_raise(self):
        with patch("subprocess.run", return_value=_cue_ok()):
            self.validator.validate({"host": "localhost"}, "config.yaml", strict=False)


class TestValidatorCUEFail(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        schema_dir = Path(self.tmpdir.name)
        (schema_dir / "config.yaml.cue").write_text('{ "host": string & != "" }')
        self.validator = Validator(schema_dir=schema_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_strict_mode_raises_cue_validation_error(self):
        with patch("subprocess.run", return_value=_cue_fail("field missing")):
            with self.assertRaises(CUEValidationError):
                self.validator.validate({}, "config.yaml", strict=True)

    def test_warn_mode_does_not_raise(self):
        with patch("subprocess.run", return_value=_cue_fail("field missing")):
            # Must not raise — only warn
            self.validator.validate({}, "config.yaml", strict=False)

    def test_error_message_included_in_exception(self):
        with patch("subprocess.run", return_value=_cue_fail("host is required")):
            with self.assertRaises(CUEValidationError) as cm:
                self.validator.validate({}, "config.yaml", strict=True)
        self.assertIn("host is required", str(cm.exception))


class TestValidatorFallback(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        schema_dir = Path(self.tmpdir.name)
        # Only default.cue present, not config.yaml.cue
        (schema_dir / "default.cue").write_text('{}')
        self.validator = Validator(schema_dir=schema_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_falls_back_to_default_cue(self):
        with patch("subprocess.run", return_value=_cue_ok()):
            # Should not raise — default.cue is used as fallback
            self.validator.validate({"any": "data"}, "unknown.yaml", strict=True)


class TestValidatorTimeout(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        schema_dir = Path(self.tmpdir.name)
        (schema_dir / "cfg.yaml.cue").write_text('{}')
        self.validator = Validator(schema_dir=schema_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_timeout_handled_gracefully(self):
        """subprocess.TimeoutExpired must not propagate as unhandled exception: a cue program that
        cannot be run is "cannot validate", said as CUEUnavailable."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["cue"], timeout=15),
        ):
            with self.assertRaises(CUEUnavailable):
                self.validator.validate({"k": "v"}, "cfg.yaml", strict=False)

    def test_a_missing_cue_program_is_not_a_passed_validation(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("cue")):
            with self.assertRaises(CUEUnavailable) as caught:
                self.validator.validate({"k": "v"}, "cfg.yaml", strict=True)
        self.assertIn("cannot validate cfg.yaml", str(caught.exception))

    def test_an_operator_who_named_the_mode_gets_files_written_unvalidated_and_it_shows(self):
        from app import dependencies

        dependencies.reset()
        with patch("subprocess.run", side_effect=FileNotFoundError("cue")),                 patch.dict("os.environ", {"VARTRACK_ALLOW_DEGRADED": "schema_validation"}):
            self.validator.validate({"k": "v"}, "cfg.yaml", strict=True)    # no error
        self.assertIn("schema_validation", dependencies.active_modes())
        dependencies.reset()

    def test_no_schema_is_still_nothing_to_check(self):
        Validator(schema_dir=None).validate({"k": "v"}, "cfg.yaml", strict=True)


if __name__ == "__main__":
    unittest.main()
