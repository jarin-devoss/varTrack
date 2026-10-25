"""
tests/unit/test_file_collector.py

file_collector hands the pushed commit to the extractor for every strategy.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.pipeline.git_extractor import FileContent  # noqa: E402
from app.tasks.file_collector import collect  # noqa: E402

URL = "https://github.com/org/repo"
SHA = "d" * 40


class TestRevisionIsPassedOn(unittest.TestCase):

    def test_single_file(self):
        with patch("app.tasks.file_collector.GitExtractor") as cls:
            cls.return_value.extract_file.return_value = "a: 1"
            out = collect({}, "github", {"file_name": "c.yaml", "token": "t"}, URL, "refs/heads/main", revision=SHA)
        self.assertEqual(out, [("c.yaml", "a: 1")])
        cls.return_value.extract_file.assert_called_once_with(
            URL, "refs/heads/main", "c.yaml", token="t", revision=SHA)

    def test_file_path_map(self):
        with patch("app.tasks.file_collector.GitExtractor") as cls:
            cls.return_value.extract_file.return_value = "a: 1"
            collect({}, "github", {"file_path_map": {"prod": "p.yaml", "dev": "d.yaml"}}, URL, "main", revision=SHA)
        revisions = {c.kwargs["revision"] for c in cls.return_value.extract_file.call_args_list}
        self.assertEqual(revisions, {SHA})

    def test_changed_files(self):
        with patch("app.tasks.file_collector.GitExtractor") as cls, \
                patch("app.tasks.file_collector.extract_changed_filenames", return_value=["a.yaml"]):
            cls.return_value.extract_changed_files.return_value = [FileContent("a.yaml", "x: 1")]
            out = collect({}, "github", {}, URL, "main", revision=SHA)
        self.assertEqual(out, [("a.yaml", "x: 1")])
        cls.return_value.extract_changed_files.assert_called_once_with(
            URL, "main", ["a.yaml"], token=None, revision=SHA)

    def test_no_revision_means_resolve_the_ref(self):
        with patch("app.tasks.file_collector.GitExtractor") as cls:
            cls.return_value.extract_file.return_value = "a: 1"
            collect({}, "github", {"file_name": "c.yaml"}, URL, "main")
        self.assertIsNone(cls.return_value.extract_file.call_args.kwargs["revision"])
