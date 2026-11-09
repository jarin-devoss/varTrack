"""
tests/unit/test_file_collector.py

file_collector hands the pushed commit to the extractor for the file a rule names, and reads nothing else.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.tasks.file_collector import NoFileNamed, collect  # noqa: E402

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

    def test_a_push_that_touched_other_files_does_not_sync_them(self):
        """What a push changed does not decide what a rule reads: only the file it names is fetched."""
        payload = {"commits": [{"added": ["README.md", "scripts/x.sh"], "modified": ["c.yaml"], "removed": []}]}
        with patch("app.tasks.file_collector.GitExtractor") as cls:
            cls.return_value.extract_file.return_value = "a: 1"
            out = collect(payload, "github", {"file_name": "c.yaml"}, URL, "main", revision=SHA)
        self.assertEqual(out, [("c.yaml", "a: 1")])
        self.assertEqual([c.args[2] for c in cls.return_value.extract_file.call_args_list], ["c.yaml"])

    def test_a_rule_that_names_no_file_reads_nothing_and_says_so(self):
        payload = {"commits": [{"added": ["a.yaml"], "modified": [], "removed": []}]}
        with patch("app.tasks.file_collector.GitExtractor") as cls:
            with self.assertRaises(NoFileNamed) as caught:
                collect(payload, "github", {"platform": "gitea", "datasource": "s3"}, URL, "main", revision=SHA)
        self.assertIn("gitea/s3 names no file to read", str(caught.exception))
        cls.return_value.extract_file.assert_not_called()

    def test_no_revision_means_resolve_the_ref(self):
        with patch("app.tasks.file_collector.GitExtractor") as cls:
            cls.return_value.extract_file.return_value = "a: 1"
            collect({}, "github", {"file_name": "c.yaml"}, URL, "main")
        self.assertIsNone(cls.return_value.extract_file.call_args.kwargs["revision"])
