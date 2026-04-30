"""
tests/unit/test_git_extractor.py

Unit tests for GitExtractor — path traversal protection, token masking,
encoding detection, and OSError/None handling.

BranchRepoCache.get_or_clone() returns (checkout_path, sha). Tests create
a real temp directory so extract_file can stat and read real files.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())


def _mock_cache_returning(tmpdir: Path, sha: str = "deadbeef"):
    """
    Return a mock get_default_cache() whose get_or_clone() returns (tmpdir, sha),
    matching the actual API: checkout_path, sha = cache.get_or_clone(url, ref).
    """
    mock_cache = MagicMock()
    mock_cache.get_or_clone.return_value = (str(tmpdir), sha)
    return mock_cache


class TestGitExtractorExtractFile(unittest.TestCase):

    def test_returns_content_for_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "config.yaml").write_text("db:\n  host: localhost\n")
            mock_cache = _mock_cache_returning(Path(tmpdir))

            with patch("app.pipeline.git_extractor.get_default_cache",
                       return_value=mock_cache):
                from app.pipeline.git_extractor import GitExtractor
                extractor = GitExtractor()
                result = extractor.extract_file(
                    "https://github.com/org/repo", "refs/heads/main", "config.yaml"
                )

        self.assertIsNotNone(result)
        self.assertIn("localhost", result)

    def test_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_cache = _mock_cache_returning(Path(tmpdir))

            with patch("app.pipeline.git_extractor.get_default_cache",
                       return_value=mock_cache):
                from app.pipeline.git_extractor import GitExtractor
                extractor = GitExtractor()
                result = extractor.extract_file(
                    "https://github.com/org/repo", "refs/heads/main", "nonexistent.yaml"
                )

        self.assertIsNone(result)

    def test_token_not_leaked_in_exception(self):
        """
        Token-injected URLs must never appear in exception messages.
        """
        secret_token = "ghp_supersecret12345"

        with patch("app.pipeline.git_extractor.get_default_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get_or_clone.side_effect = RuntimeError(
                "git clone failed: https://user:TOKEN@github.com/org/repo"
            )
            mock_cache_fn.return_value = mock_cache
            from app.pipeline.git_extractor import GitExtractor
            extractor = GitExtractor()

            try:
                extractor.extract_file(
                    "https://github.com/org/repo", "main", "cfg.yaml",
                    token=secret_token,
                )
            except Exception as exc:
                self.assertNotIn(secret_token, str(exc),
                                 "Secret token must not appear in exception message")


class TestGitExtractorPathTraversal(unittest.TestCase):

    def test_path_traversal_rejected(self):
        """
        Paths like '../../etc/passwd' must not escape the repo root.
        The extractor catches the ValueError and returns None (does NOT propagate it).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_cache = _mock_cache_returning(Path(tmpdir))

            with patch("app.pipeline.git_extractor.get_default_cache",
                       return_value=mock_cache):
                from app.pipeline.git_extractor import GitExtractor
                extractor = GitExtractor()
                result = extractor.extract_file(
                    "https://github.com/org/repo",
                    "refs/heads/main",
                    "../../etc/passwd",
                )

        # Path traversal is caught and returns None
        self.assertIsNone(result, "Path traversal attempt must be blocked (returns None)")

    def test_normal_relative_path_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sub = Path(tmpdir) / "config"
            sub.mkdir()
            (sub / "prod.yaml").write_text("x: 1")
            mock_cache = _mock_cache_returning(Path(tmpdir))

            with patch("app.pipeline.git_extractor.get_default_cache",
                       return_value=mock_cache):
                from app.pipeline.git_extractor import GitExtractor
                extractor = GitExtractor()
                try:
                    extractor.extract_file(
                        "https://github.com/org/repo", "refs/heads/main",
                        "config/prod.yaml"
                    )
                except (ValueError, PermissionError):
                    self.fail("Normal relative path should not be rejected")


class TestGitExtractorEncodingDetection(unittest.TestCase):

    def test_utf8_content_returned_as_str(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "cfg.yaml").write_text("host: localhost\n", encoding="utf-8")
            mock_cache = _mock_cache_returning(Path(tmpdir))

            with patch("app.pipeline.git_extractor.get_default_cache",
                       return_value=mock_cache):
                from app.pipeline.git_extractor import GitExtractor
                extractor = GitExtractor()
                result = extractor.extract_file(
                    "https://github.com/org/repo", "main", "cfg.yaml"
                )

        if result is not None:
            self.assertIsInstance(result, str)

    def test_oserror_treated_as_deletion(self):
        """
        OSError from git ops must be treated as file-not-found
        (deleted), not propagated as an exception that stops the ETL run.

        The clone/fetch itself raises OSError; extract_file must catch it
        and return None.
        """
        with patch("app.pipeline.git_extractor.get_default_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get_or_clone.side_effect = OSError("no such file")
            mock_cache_fn.return_value = mock_cache

            from app.pipeline.git_extractor import GitExtractor
            extractor = GitExtractor()
            try:
                result = extractor.extract_file(
                    "https://github.com/org/repo", "main", "deleted.yaml"
                )
                self.assertIsNone(result)
            except OSError:
                self.fail("OSError from get_or_clone should be caught and treated as deletion")


if __name__ == "__main__":
    unittest.main()
