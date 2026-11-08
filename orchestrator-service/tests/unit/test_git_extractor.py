"""
tests/unit/test_git_extractor.py

Unit tests for GitExtractor — which commit is read, path traversal protection,
token masking, encoding detection and the failure semantics — against a fake
RepoMirrors (the mirrors themselves are tested in test_repo_mirror.py).
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.dependencies import SourceUnavailable  # noqa: E402
from app.utils.git.mirror import MirrorError, clean_path  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40
URL = "https://github.com/org/repo"


class FakeMirrors:
    """A mirror holding `files` at SHA; records what was asked of it."""

    def __init__(self, files=None, resolves=SHA, present=(SHA,)):
        self.files = files or {}
        self.resolves = resolves
        self.present = set(present)
        self.calls: list[tuple] = []

    def resolve(self, url, token, ref):
        self.calls.append(("resolve", ref))
        if isinstance(self.resolves, Exception):
            raise self.resolves
        return self.resolves

    def ensure(self, url, token):
        self.calls.append(("ensure",))

    def has_commit(self, url, sha):
        return sha in self.present

    def fetch(self, url, token, sha=None, ref=None):
        self.calls.append(("fetch", sha, ref))
        self.present.add(sha)

    def read(self, url, sha, file_path):
        clean_path(file_path)
        self.calls.append(("read", sha, file_path))
        value = self.files.get(file_path)
        if isinstance(value, Exception):
            raise value
        return value


def _extractor(mirrors):
    from app.pipeline.git_extractor import GitExtractor
    patcher = patch("app.pipeline.git_extractor.get_default_mirrors", return_value=mirrors)
    patcher.start()
    return GitExtractor(), patcher


class _Base(unittest.TestCase):
    def use(self, mirrors):
        extractor, patcher = _extractor(mirrors)
        self.addCleanup(patcher.stop)
        return extractor


class TestWhichCommitIsRead(_Base):

    def test_the_pushed_commit_is_read_not_the_branch_head(self):
        mirrors = FakeMirrors({"c.yaml": b"v: 1"}, resolves=OTHER, present=(SHA,))
        result = self.use(mirrors).extract_file(URL, "refs/heads/main", "c.yaml", revision=SHA)
        self.assertEqual(result, "v: 1")
        self.assertIn(("read", SHA, "c.yaml"), mirrors.calls)
        self.assertNotIn(("resolve", "refs/heads/main"), mirrors.calls)

    def test_without_a_revision_the_ref_is_resolved(self):
        mirrors = FakeMirrors({"c.yaml": b"v: 1"})
        self.use(mirrors).extract_file(URL, "main", "c.yaml")
        self.assertEqual(mirrors.calls[0], ("resolve", "main"))
        self.assertIn(("read", SHA, "c.yaml"), mirrors.calls)

    def test_the_all_zero_id_of_a_deleted_ref_is_not_a_revision(self):
        mirrors = FakeMirrors({"c.yaml": b"x"})
        self.use(mirrors).extract_file(URL, "main", "c.yaml", revision="0" * 40)
        self.assertEqual(mirrors.calls[0], ("resolve", "main"))

    def test_a_commit_missing_from_the_mirror_is_fetched_first(self):
        mirrors = FakeMirrors({"c.yaml": b"x"}, present=())
        self.use(mirrors).extract_file(URL, "refs/heads/main", "c.yaml", revision=SHA)
        self.assertEqual(
            [c[0] for c in mirrors.calls], ["ensure", "fetch", "read"],
        )
        self.assertIn(("fetch", SHA, "refs/heads/main"), mirrors.calls)

    def test_a_commit_already_in_the_mirror_is_not_fetched(self):
        mirrors = FakeMirrors({"c.yaml": b"x"})
        self.use(mirrors).extract_file(URL, "main", "c.yaml", revision=SHA)
        self.assertFalse([c for c in mirrors.calls if c[0] == "fetch"])

    def test_every_file_of_a_batch_is_read_at_the_same_commit(self):
        mirrors = FakeMirrors({"a.yaml": b"a", "b.yaml": b"b"})
        out = self.use(mirrors).extract_changed_files(
            URL, "main", ["a.yaml", "b.yaml", "gone.yaml"], revision=SHA,
        )
        self.assertEqual([(f.path, f.content) for f in out],
                         [("a.yaml", "a"), ("b.yaml", "b"), ("gone.yaml", None)])
        self.assertEqual({c[1] for c in mirrors.calls if c[0] == "read"}, {SHA})


class TestExtractFile(_Base):

    def test_returns_content_for_existing_file(self):
        result = self.use(FakeMirrors({"config.yaml": b"db:\n  host: localhost\n"})).extract_file(
            URL, "refs/heads/main", "config.yaml")
        self.assertIn("localhost", result)

    def test_returns_none_for_missing_file(self):
        self.assertIsNone(self.use(FakeMirrors()).extract_file(URL, "refs/heads/main", "nonexistent.yaml"))

    def test_unreachable_repository_is_an_error_not_a_missing_file(self):
        mirrors = FakeMirrors(resolves=MirrorError("git ls-remote failed"))
        with self.assertRaises(SourceUnavailable) as caught:
            self.use(mirrors).extract_file(URL, "main", "c.yaml")
        self.assertIn(URL, str(caught.exception))
        self.assertIn("git ls-remote failed", str(caught.exception))

    def test_a_read_error_is_raised_not_taken_for_a_deletion(self):
        mirrors = FakeMirrors({"c.yaml": MirrorError("commit not in mirror")})
        with self.assertRaises(MirrorError):
            self.use(mirrors).extract_file(URL, "main", "c.yaml")

    def test_a_batch_file_that_cannot_be_read_fails_the_batch(self):
        mirrors = FakeMirrors({"bad.yaml": MirrorError("boom"), "ok.yaml": b"ok"})
        with self.assertRaises(SourceUnavailable) as caught:
            self.use(mirrors).extract_changed_files(URL, "main", ["bad.yaml", "ok.yaml"])
        self.assertIn("bad.yaml", str(caught.exception))

    def test_an_unreachable_repository_fails_the_whole_batch(self):
        mirrors = FakeMirrors(resolves=MirrorError("down"))
        with self.assertRaises(SourceUnavailable):
            self.use(mirrors).extract_changed_files(URL, "main", ["a", "b"])

    def test_a_missing_file_in_a_batch_is_still_none(self):
        mirrors = FakeMirrors({"gone.yaml": None, "ok.yaml": b"ok"})
        out = self.use(mirrors).extract_changed_files(URL, "main", ["gone.yaml", "ok.yaml"])
        self.assertEqual([(f.path, f.content) for f in out], [("gone.yaml", None), ("ok.yaml", "ok")])

    def test_oserror_is_treated_as_unreachable(self):
        mirrors = FakeMirrors(resolves=OSError("no such file"))
        with self.assertRaises(SourceUnavailable):
            self.use(mirrors).extract_file(URL, "main", "deleted.yaml")

    def test_a_password_in_the_error_is_hidden(self):
        mirrors = FakeMirrors(resolves=MirrorError("fatal: unable to access 'https://oauth2:s3cret@host/o/r.git'"))
        with self.assertRaises(SourceUnavailable) as caught:
            self.use(mirrors).extract_file(URL, "main", "c.yaml")
        self.assertNotIn("s3cret", str(caught.exception))

    def test_empty_batch_touches_nothing(self):
        mirrors = FakeMirrors()
        self.assertEqual(self.use(mirrors).extract_changed_files(URL, "main", []), [])
        self.assertEqual(mirrors.calls, [])


class TestToken(_Base):

    def test_token_is_handed_to_the_mirror_and_not_logged(self):
        secret = "ghp_supersecret12345"
        mirrors = FakeMirrors({"c.yaml": b"x"})
        seen = []
        mirrors.ensure = lambda url, token: seen.append(token)
        with self.assertLogs("app.pipeline.git_extractor", level="DEBUG") as logs:
            self.use(mirrors).extract_file(URL, "main", "c.yaml", token=secret)
        self.assertEqual(seen, [secret])
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_get_commit_sha_failure_does_not_carry_git_text(self):
        secret = "ghp_supersecret12345"
        mirrors = FakeMirrors(resolves=MirrorError(f"fatal: {secret}"))
        extractor = self.use(mirrors)
        with patch("app.utils.redis_cache.get_cache", return_value=None):
            with self.assertRaises(RuntimeError) as err:
                extractor.get_commit_sha(URL, "main", token=secret)
        self.assertNotIn(secret, str(err.exception))
        self.assertIsNone(err.exception.__cause__)

    def test_get_commit_sha_asks_the_remote(self):
        mirrors = FakeMirrors()
        with patch("app.utils.redis_cache.get_cache", return_value=None):
            sha = self.use(mirrors).get_commit_sha(URL, "main")
        self.assertEqual(sha, SHA)
        self.assertEqual(mirrors.calls, [("resolve", "refs/heads/main")])


    def test_a_cached_head_is_what_self_heal_gets_but_not_what_a_sync_asks_for(self):
        """The head of a branch is remembered for two minutes (self-heal asks every few minutes and need not ask git
        each time). A Sync a person presses right after a push has to read that push, not the commit from before it."""
        class Cache:
            def __init__(self):
                self.values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, ttl=None):
                self.values[key] = value

        cache = Cache()
        mirrors = FakeMirrors()
        with patch("app.utils.redis_cache.get_cache", return_value=cache):
            extractor = self.use(mirrors)
            assert extractor.get_commit_sha(URL, "main") == SHA                 # asks git, remembers it
            cache.values = {k: "0" * 40 for k in cache.values}                  # ...and the branch moves on
            assert extractor.get_commit_sha(URL, "main") == "0" * 40            # the cached head, as before
            assert extractor.get_commit_sha(URL, "main", fresh=True) == SHA     # a sync reads git again
            assert extractor.get_commit_sha(URL, "main") == SHA                 # and what it read is remembered

    def test_a_fresh_head_does_not_need_the_cache_to_work(self):
        mirrors = FakeMirrors()
        with patch("app.utils.redis_cache.get_cache", return_value=None):
            assert self.use(mirrors).get_commit_sha(URL, "main", fresh=True) == SHA


class TestPathTraversal(_Base):

    def test_path_traversal_rejected(self):
        mirrors = FakeMirrors()
        self.assertIsNone(self.use(mirrors).extract_file(URL, "refs/heads/main", "../../etc/passwd"))
        self.assertEqual(mirrors.calls, [])

    def test_a_batch_path_traversal_is_none(self):
        out = self.use(FakeMirrors()).extract_changed_files(URL, "main", ["../x"])
        self.assertEqual([(f.path, f.content) for f in out], [("../x", None)])

    def test_normal_relative_path_accepted(self):
        mirrors = FakeMirrors({"config/prod.yaml": b"x: 1"})
        self.assertEqual(self.use(mirrors).extract_file(URL, "main", "config/prod.yaml"), "x: 1")


class TestEncoding(_Base):

    def _read(self, raw: bytes):
        return self.use(FakeMirrors({"f": raw})).extract_file(URL, "main", "f")

    def test_utf8(self):
        self.assertEqual(self._read("host: ü\n".encode()), "host: ü\n")

    def test_utf8_bom_is_dropped(self):
        self.assertEqual(self._read(b"\xef\xbb\xbfa: 1"), "a: 1")

    def test_utf16_bom(self):
        self.assertEqual(self._read("a: 1".encode("utf-16")), "a: 1")

    def test_utf32_bom(self):
        self.assertEqual(self._read("a: 1".encode("utf-32")), "a: 1")

    def test_invalid_bytes_are_replaced_not_raised(self):
        self.assertIn("�", self._read(b"a: \xff\xfe\xfa"[:0] + b"a: \x80\x81"))


if __name__ == "__main__":
    unittest.main()
