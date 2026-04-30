"""
tests/unit/test_models.py

Unit tests for pipeline data classes:
  PayloadContext, ETLFile, ETLResult, SyncResult
"""
from __future__ import annotations

import unittest
from dataclasses import asdict

from app.pipeline.models import ETLFile, ETLResult, PayloadContext, SyncResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(**kw) -> PayloadContext:
    defaults = dict(
        platform="github",
        repo_url="https://github.com/org/repo",
        ref="refs/heads/main",
        branch="main",
        commit_sha="abc123",
        tag=None,
        pr_number=None,
        rule_config={},
        parsed_payload={},
    )
    defaults.update(kw)
    return PayloadContext(**defaults)


def _make_file(**kw) -> ETLFile:
    defaults = dict(
        file_path="config.yaml",
        env="production",
        flat_data={"db.host": "localhost"},
        root_key="vartrack",
    )
    defaults.update(kw)
    return ETLFile(**defaults)


def _make_sync(**kw) -> SyncResult:
    defaults = dict(file_path="config.yaml", env="production", status="ok")
    defaults.update(kw)
    return SyncResult(**defaults)


# ── PayloadContext ─────────────────────────────────────────────────────────────

class TestPayloadContext(unittest.TestCase):

    def test_defaults_set_correctly(self):
        ctx = _make_ctx()
        self.assertEqual(ctx.platform, "github")
        self.assertEqual(ctx.branch, "main")
        self.assertIsNone(ctx.tag)
        self.assertIsNone(ctx.pr_number)

    def test_tag_set(self):
        ctx = _make_ctx(tag="v1.2.3")
        self.assertEqual(ctx.tag, "v1.2.3")

    def test_pr_number_set(self):
        ctx = _make_ctx(pr_number="42")
        self.assertEqual(ctx.pr_number, "42")

    def test_rule_config_defaults_empty(self):
        ctx = _make_ctx()
        self.assertEqual(ctx.rule_config, {})

    def test_parsed_payload_stored(self):
        payload = {"ref": "refs/heads/main", "repository": {"full_name": "org/repo"}}
        ctx = _make_ctx(parsed_payload=payload)
        self.assertEqual(ctx.parsed_payload["ref"], "refs/heads/main")


# ── ETLFile ────────────────────────────────────────────────────────────────────

class TestETLFile(unittest.TestCase):

    def test_defaults(self):
        f = _make_file()
        self.assertEqual(f.validation, "ok")
        self.assertEqual(f.error, "")

    def test_validation_states(self):
        for state in ("ok", "warn", "failed"):
            f = _make_file(validation=state)
            self.assertEqual(f.validation, state)

    def test_flat_data_stored(self):
        data = {"key.one": "val1", "key.two": "val2"}
        f = _make_file(flat_data=data)
        self.assertEqual(f.flat_data["key.one"], "val1")

    def test_error_message_stored(self):
        f = _make_file(validation="failed", error="CUE validation failed: field missing")
        self.assertIn("CUE", f.error)


# ── ETLResult ─────────────────────────────────────────────────────────────────

class TestETLResult(unittest.TestCase):

    def test_empty_by_default(self):
        r = ETLResult()
        self.assertEqual(r.files, [])
        self.assertEqual(r.skipped, [])
        self.assertEqual(r.errors, [])

    def test_mutable_lists_not_shared(self):
        # Each instance must have its own lists (dataclass default_factory).
        r1 = ETLResult()
        r2 = ETLResult()
        r1.files.append(_make_file())
        self.assertEqual(len(r2.files), 0, "ETLResult instances must not share lists")

    def test_files_appended(self):
        r = ETLResult()
        r.files.append(_make_file(file_path="a.yaml"))
        r.files.append(_make_file(file_path="b.yaml"))
        self.assertEqual(len(r.files), 2)

    def test_errors_tracked(self):
        r = ETLResult()
        r.errors.append("unexpected error parsing file.xml: ...")
        self.assertEqual(len(r.errors), 1)

    def test_skipped_tracked(self):
        r = ETLResult()
        r.skipped.append("config.yaml (empty file)")
        self.assertTrue(r.skipped)


# ── SyncResult ────────────────────────────────────────────────────────────────

class TestSyncResult(unittest.TestCase):

    def test_valid_status_values(self):
        valid = ("ok", "empty", "validation_failed", "error", "skipped", "aborted")
        for s in valid:
            sr = _make_sync(status=s)
            self.assertEqual(sr.status, s)

    def test_defaults_zero(self):
        sr = _make_sync()
        self.assertEqual(sr.written, 0)
        self.assertEqual(sr.pruned, 0)
        self.assertEqual(sr.sync_mode, "")
        self.assertEqual(sr.error, "")
        self.assertEqual(sr.root_key, "")

    def test_write_counts(self):
        sr = _make_sync(status="ok", written=42, pruned=3, sync_mode="GIT_UPSERT_ALL")
        self.assertEqual(sr.written, 42)
        self.assertEqual(sr.pruned, 3)
        self.assertEqual(sr.sync_mode, "GIT_UPSERT_ALL")

    def test_aborted_status(self):
        # "aborted" must be a valid SyncResult status so
        # callers can distinguish rollback failures from legitimate skips.
        sr = _make_sync(status="aborted", error="rollback after write failure")
        self.assertEqual(sr.status, "aborted")
        self.assertIn("rollback", sr.error)

    def test_asdict_serialisable(self):
        sr = _make_sync(status="ok", written=5)
        d = asdict(sr)
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["written"], 5)


if __name__ == "__main__":
    unittest.main()
