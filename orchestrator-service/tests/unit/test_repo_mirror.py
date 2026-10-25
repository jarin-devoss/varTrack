"""
tests/unit/test_repo_mirror.py

RepoMirrors against real git repositories on disk (no network): a work
repository stands in for the remote, the mirror is cloned from its path.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils.git import mirror as mirror_mod
from app.utils.git.mirror import (
    MirrorError,
    RepoMirrors,
    clean_path,
    is_commit_sha,
    remove_old_layout,
    strip_userinfo,
)

_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *_IDENT, *args], cwd=cwd, check=True, capture_output=True)
    return out.stdout.decode().strip()


def _commit(work: Path, files: dict[str, str | None], message: str = "c") -> str:
    for name, body in files.items():
        target = work / name
        if body is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body.encode() if isinstance(body, str) else body)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path):
    work = tmp_path / "remote"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    return work


@pytest.fixture
def mirrors(tmp_path):
    return RepoMirrors(root=tmp_path / "cache", max_repos=10)


def test_reads_a_file_at_its_commit_after_the_branch_moved(remote, mirrors):
    first = _commit(remote, {"app/config.yaml": "v: 1\n"})
    url = str(remote)
    mirrors.ensure(url, None)

    second = _commit(remote, {"app/config.yaml": "v: 2\n"})
    mirrors.fetch(url, None)

    assert mirrors.read(url, first, "app/config.yaml") == b"v: 1\n"
    assert mirrors.read(url, second, "app/config.yaml") == b"v: 2\n"


def test_resolve_follows_the_remote_without_a_clone(remote, mirrors):
    sha = _commit(remote, {"a.txt": "a"})
    assert mirrors.resolve(str(remote), None, "main") == sha
    assert mirrors.resolve(str(remote), None, "refs/heads/main") == sha
    assert not mirrors.path(str(remote)).exists()

    newer = _commit(remote, {"a.txt": "b"})
    assert mirrors.resolve(str(remote), None, "main") == newer


def test_resolve_gives_the_commit_of_an_annotated_tag(remote, mirrors):
    sha = _commit(remote, {"a.txt": "a"})
    _git(remote, "tag", "-a", "v1", "-m", "release")
    _commit(remote, {"a.txt": "b"})
    assert mirrors.resolve(str(remote), None, "v1") == sha
    assert mirrors.resolve(str(remote), None, "refs/tags/v1") == sha


def test_resolve_unknown_ref_raises(remote, mirrors):
    _commit(remote, {"a.txt": "a"})
    with pytest.raises(MirrorError, match="not found"):
        mirrors.resolve(str(remote), None, "nope")


def test_has_commit_and_fetch_bring_a_new_commit(remote, mirrors):
    _commit(remote, {"a.txt": "a"})
    url = str(remote)
    mirrors.ensure(url, None)
    later = _commit(remote, {"a.txt": "b"})

    assert not mirrors.has_commit(url, later)
    mirrors.fetch(url, None, sha=later, ref="refs/heads/main")
    assert mirrors.has_commit(url, later)


def test_fetch_gets_a_commit_that_is_on_no_branch(remote, mirrors):
    _commit(remote, {"a.txt": "a"})
    url = str(remote)
    mirrors.ensure(url, None)
    _git(remote, "switch", "-q", "-c", "topic")
    topic = _commit(remote, {"a.txt": "topic"})
    _git(remote, "switch", "-q", "main")
    _git(remote, "update-ref", "refs/pull/1/head", topic)
    _git(remote, "branch", "-q", "-D", "topic")
    _git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")

    mirrors.fetch(url, None, sha=topic, ref="refs/pull/1/head")
    assert mirrors.read(url, topic, "a.txt") == b"topic"


def test_fetch_of_a_missing_commit_raises(remote, mirrors):
    _commit(remote, {"a.txt": "a"})
    url = str(remote)
    mirrors.ensure(url, None)
    with pytest.raises(MirrorError):
        mirrors.fetch(url, None, sha="1" * 40, ref="refs/heads/main")


def test_missing_file_is_none_but_missing_commit_raises(remote, mirrors):
    sha = _commit(remote, {"a.txt": "a", "dir/b.txt": "b"})
    url = str(remote)
    mirrors.ensure(url, None)

    assert mirrors.read(url, sha, "nope.txt") is None
    assert mirrors.read(url, sha, "dir") is None
    with pytest.raises(MirrorError):
        mirrors.read(url, "2" * 40, "a.txt")


def test_a_deleted_file_is_none_at_the_commit_that_deleted_it(remote, mirrors):
    before = _commit(remote, {"a.txt": "a", "b.txt": "b"})
    after = _commit(remote, {"a.txt": None})
    url = str(remote)
    mirrors.ensure(url, None)

    assert mirrors.read(url, before, "a.txt") == b"a"
    assert mirrors.read(url, after, "a.txt") is None


def test_bytes_come_back_untouched(remote, mirrors):
    raw = b"\xef\xbb\xbfkey: v\r\n"
    work = remote / "bom.yaml"
    work.write_bytes(raw)
    _git(remote, "-c", "core.autocrlf=false", "add", "-A")
    _git(remote, "commit", "-q", "-m", "bom")
    sha = _git(remote, "rev-parse", "HEAD")
    mirrors.ensure(str(remote), None)
    assert mirrors.read(str(remote), sha, "bom.yaml") == raw


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "a\\b", "", ".."])
def test_paths_outside_the_repository_are_refused(bad, remote, mirrors):
    sha = _commit(remote, {"a.txt": "a"})
    mirrors.ensure(str(remote), None)
    with pytest.raises(ValueError):
        mirrors.read(str(remote), sha, bad)


def test_clean_path_normalises():
    assert clean_path("a/./b//c.yaml") == "a/b/c.yaml"


def test_is_commit_sha():
    assert is_commit_sha("a" * 40)
    assert is_commit_sha("a" * 64)
    assert not is_commit_sha("0" * 40)      # a deleted ref in a push event
    assert not is_commit_sha("main")
    assert not is_commit_sha("A" * 40)
    assert not is_commit_sha(None)


def test_the_credentials_in_a_url_do_not_change_the_mirror():
    assert strip_userinfo("https://user:pw@host/org/r.git") == "https://host/org/r.git"
    assert strip_userinfo("https://host/org/r.git") == "https://host/org/r.git"
    assert RepoMirrors.key("https://u:p@host/r") == RepoMirrors.key("https://host/r")
    assert RepoMirrors.key("https://host/a") != RepoMirrors.key("https://host/b")


def test_a_broken_directory_is_replaced(remote, mirrors):
    sha = _commit(remote, {"a.txt": "a"})
    url = str(remote)
    path = mirrors.path(url)
    path.mkdir(parents=True)
    (path / "junk").write_text("half a clone")

    mirrors.ensure(url, None)
    assert mirrors.read(url, sha, "a.txt") == b"a"


def test_ensure_twice_clones_once(remote, mirrors):
    _commit(remote, {"a.txt": "a"})
    url = str(remote)
    mirrors.ensure(url, None)
    with patch.object(RepoMirrors, "_run", side_effect=AssertionError("cloned again")):
        mirrors.ensure(url, None)


def test_least_recently_used_mirrors_are_evicted(tmp_path):
    cache = RepoMirrors(root=tmp_path / "cache", max_repos=2)
    urls = []
    for i in range(3):
        work = tmp_path / f"r{i}"
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        _commit(work, {"a.txt": str(i)})
        urls.append(str(work))
        cache.ensure(urls[-1], None)
        used = cache.root / f"{cache.key(urls[-1])}.used"
        os.utime(used, (time.time() + i, time.time() + i))

    assert not cache.path(urls[0]).exists()
    assert cache.path(urls[1]).exists()
    assert cache.path(urls[2]).exists()


def test_a_mirror_in_use_is_not_evicted(tmp_path):
    cache = RepoMirrors(root=tmp_path / "cache", max_repos=1)
    first = tmp_path / "r0"
    second = tmp_path / "r1"
    for i, work in enumerate((first, second)):
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        _commit(work, {"a.txt": str(i)})

    cache.ensure(str(first), None)
    with cache._locked(cache.key(str(first))):
        # the lock is held by this thread, so the eviction cannot take the mirror
        cache.root.joinpath(f"{cache.key(str(first))}.used").touch()
        cache.ensure(str(second), None)
    assert cache.path(str(first)).exists()


def test_the_token_goes_in_the_environment_not_the_arguments():
    env = RepoMirrors._env("https://host/org/r.git", "s3cret")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "s3cret" not in env["GIT_CONFIG_VALUE_0"]      # base64, not plain
    assert RepoMirrors._env("ssh://host/r", "s3cret").get("GIT_CONFIG_COUNT") is None


def test_the_token_is_scrubbed_from_git_errors(mirrors):
    class Done:
        returncode = 128
        stdout = b""
        stderr = b"fatal: unable to access 'https://host/r': bad credentials s3cret"

    with patch.object(mirror_mod.subprocess, "run", return_value=Done()):
        with pytest.raises(MirrorError) as err:
            mirrors._run(["fetch"], url="https://host/r", token="s3cret")
    assert "s3cret" not in str(err.value)


def test_old_temporary_caches_are_removed_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "vt_gitcache_old"
    fresh = tmp_path / "vt_gitcache_fresh"
    other = tmp_path / "unrelated"
    for d in (old, fresh, other):
        d.mkdir()
        (d / "f").write_text("x")
    stale = time.time() - 3 * 24 * 3600
    os.utime(old, (stale, stale))

    assert remove_old_layout() == 1
    assert not old.exists()
    assert fresh.exists() and other.exists()


# ── GIT_CACHE_DIR ─────────────────────────────────────────────────────────────

def test_the_root_comes_from_git_cache_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "GIT_CACHE_DIR", str(tmp_path / "configured"))
    monkeypatch.setattr(settings, "GIT_CACHE_MAX_ENTRIES", 7)
    mirrors = RepoMirrors()
    assert mirrors.root == tmp_path / "configured" and mirrors.root.is_dir()
    assert mirrors.max_repos == 7


def test_the_default_mirrors_are_made_once_under_git_cache_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "GIT_CACHE_DIR", str(tmp_path / "configured"))
    monkeypatch.setattr(mirror_mod, "_default", None)
    monkeypatch.setattr(mirror_mod, "remove_old_layout", lambda: 0)
    first = mirror_mod.get_default_mirrors()
    assert first is mirror_mod.get_default_mirrors()
    assert first.root == tmp_path / "configured"


def test_another_process_on_the_same_root_reuses_the_mirror(remote, tmp_path):
    sha = _commit(remote, {"a.txt": "a"})
    url = str(remote)
    RepoMirrors(root=tmp_path / "cache").ensure(url, None)       # one worker clones it

    other = RepoMirrors(root=tmp_path / "cache")                 # another process starts later
    with patch.object(RepoMirrors, "_run", side_effect=AssertionError("cloned again")):
        other.ensure(url, None)
    assert other.has_commit(url, sha)
    assert other.read(url, sha, "a.txt") == b"a"
