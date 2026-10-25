"""
app/utils/git/mirror.py
───────────────────────
One bare mirror per repository, and files read from it at an exact commit.

A sync is recorded under the commit that was pushed, so it has to read that
commit's files, not whatever the branch holds a moment later. A mirror has no
working tree to move under a reader: a file is read with `git cat-file blob
<commit>:<path>`, and a commit that is not in the mirror yet is fetched first.

  resolve(url, token, ref)   a branch, tag or full ref to a commit, with
                             `git ls-remote`: no clone, no fetch
  ensure(url, token)         the mirror exists (cloned once, then reused)
  has_commit(url, sha)       the commit is in the mirror
  fetch(url, token, ...)     bring heads and tags up to date, and fetch the
                             commit itself when it is still missing
  read(url, sha, path)       the file at that commit, as bytes; None when the
                             commit has no such file

Mirrors live under GIT_CACHE_DIR, one directory per repository, keyed by the
repository's address without credentials. Processes on the same host share
them: clone and fetch hold a lock per repository (a thread lock, and a file
lock where the platform has one); reads take none, because git object reads are
safe while a fetch adds objects. The directory holds at most
GIT_CACHE_MAX_ENTRIES mirrors: past that the least recently used are removed,
unless one is in use.

Credentials are passed to git through its environment, never in the URL or on
the command line, so they are written to no config file, show in no process
list and appear in no error text.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, TypeGuard

if sys.platform != "win32":  # on Windows only the thread lock applies
    import fcntl

logger = logging.getLogger(__name__)

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_TIMEOUT_SECONDS = 300
_OLD_LAYOUT_PREFIX = "vt_gitcache_"
_OLD_LAYOUT_MAX_AGE_SECONDS = 24 * 3600


class MirrorError(RuntimeError):
    """A git operation on a mirror failed. The message never holds a credential."""


def is_commit_sha(value: object) -> TypeGuard[str]:
    """A full commit id. The all-zero id a push event carries for a deleted ref is not one."""
    return isinstance(value, str) and bool(_SHA.fullmatch(value)) and set(value) != {"0"}


def clean_path(path: str) -> str:
    """*path* as a repository-relative path, or ValueError when it could name
    anything outside the repository: absolute, with a backslash, or with `..`."""
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError(f"not a repository-relative path: {path!r}")
    normal = posixpath.normpath(path)
    if normal == ".." or normal.startswith("../") or normal == ".":
        raise ValueError(f"path escapes the repository: {path!r}")
    return normal


def strip_userinfo(url: str) -> str:
    """*url* without any user:password@ part, for keys and messages."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, tail = rest.partition("/")
    return f"{scheme}://{authority.rsplit('@', 1)[-1]}{slash}{tail}"


class RepoMirrors:
    """The bare mirrors under one directory."""

    def __init__(self, root: str | os.PathLike | None = None, max_repos: int | None = None) -> None:
        from app.config import settings

        self.root = Path(root if root is not None else settings.GIT_CACHE_DIR)
        self.max_repos = int(max_repos if max_repos is not None else settings.GIT_CACHE_MAX_ENTRIES)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_locks").mkdir(exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ── Where a mirror lives ──────────────────────────────────────────────────

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(strip_userinfo(url).encode()).hexdigest()[:16]

    def path(self, url: str) -> Path:
        return self.root / f"{self.key(url)}.git"

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve(self, url: str, token: Optional[str], ref: str) -> str:
        """The commit *ref* points to on the remote. *ref* is a full ref
        ("refs/heads/main", "refs/tags/v1") or a short name, tried as a branch
        and then as a tag. An annotated tag gives the commit it points to."""
        if ref.startswith("refs/"):
            names = [ref]
        else:
            names = [f"refs/heads/{ref}", f"refs/tags/{ref}"]
        # the peeled name (`^{}`) is what an annotated tag points to
        asked = [asked for name in names for asked in (name, name + "^{}")]
        out = self._run(["ls-remote", "--", url, *asked], url=url, token=token).stdout.decode(errors="replace")
        found: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if sha and name:
                found[name] = sha
        for name in names:
            sha = found.get(name + "^{}") or found.get(name)
            if sha:
                return sha
        raise MirrorError(f"{ref} not found in {strip_userinfo(url)}")

    def ensure(self, url: str, token: Optional[str]) -> Path:
        """The mirror of *url*, cloned the first time. A half-written or broken
        directory is replaced: the clone goes to a temporary directory and is
        moved into place only when it is whole."""
        key, path = self.key(url), self.path(url)
        with self._locked(key):
            if self._whole(path):
                self._touch(key)
                return path
            self._rmtree(path)
            tmp = self.root / f"{key}.tmp-{os.getpid()}-{threading.get_ident()}"
            self._rmtree(tmp)
            try:
                self._run(["clone", "--bare", "--quiet", "--", url, str(tmp)], url=url, token=token)
                os.replace(tmp, path)
            except BaseException:
                self._rmtree(tmp)
                raise
            self._touch(key)
            self._evict(keep=key)
        return path

    def has_commit(self, url: str, sha: str) -> bool:
        path = self.path(url)
        if not self._whole(path):
            return False
        proc = self._run(["--git-dir", str(path), "cat-file", "-t", sha], check=False)
        return proc.returncode == 0 and proc.stdout.strip() == b"commit"

    def fetch(self, url: str, token: Optional[str], sha: Optional[str] = None, ref: Optional[str] = None) -> None:
        """Bring the mirror's heads and tags up to date. With *sha*, the commit
        has to be there afterwards: when the general fetch does not bring it
        (a pull request's head, say), it is fetched by the ref and then by the
        commit itself."""
        key, path = self.key(url), self.path(url)
        with self._locked(key):
            git_dir = ["--git-dir", str(path)]
            self._run([*git_dir, "fetch", "--quiet", "--prune", "--force", "origin",
                       "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], url=url, token=token)
            if sha and not self.has_commit(url, sha):
                targets = ([ref] if ref and ref.startswith("refs/") else []) + [sha]
                for target in targets:
                    proc = self._run([*git_dir, "fetch", "--quiet", "origin", target], url=url, token=token, check=False)
                    if proc.returncode == 0 and self.has_commit(url, sha):
                        break
                else:
                    raise MirrorError(f"commit {sha} not found in {strip_userinfo(url)}")
            self._touch(key)

    def read(self, url: str, sha: str, file_path: str) -> Optional[bytes]:
        """The bytes of *file_path* at commit *sha*, or None when that commit
        has no such file (or the path is a directory). Raises MirrorError when
        the commit itself is not in the mirror, and ValueError for a path that
        is not repository-relative."""
        rel = clean_path(file_path)
        path = self.path(url)
        proc = self._run(["--git-dir", str(path), "cat-file", "blob", f"{sha}:{rel}"], check=False)
        if proc.returncode == 0:
            self._touch(self.key(url))
            return proc.stdout
        if not self.has_commit(url, sha):
            raise MirrorError(f"commit {sha} is not in the mirror of {strip_userinfo(url)}")
        return None

    # ── git ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _env(url: str, token: Optional[str]) -> dict[str, str]:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10",
        }
        if token and url.startswith("https://"):
            basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
        return env

    def _run(self, args: list[str], *, url: str = "", token: Optional[str] = None,
             check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(["git", *args], capture_output=True, env=self._env(url, token),
                                  timeout=_GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MirrorError(f"git {_verb(args)} could not run: {type(exc).__name__}") from None
        if check and proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            if token:
                stderr = stderr.replace(token, "***")
            raise MirrorError(f"git {_verb(args)} failed: {stderr}")
        return proc

    # ── locks ─────────────────────────────────────────────────────────────────

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        """Exclusive on this repository, across threads and, where the platform
        has file locks, across the processes that share the directory."""
        with self._lock_for(key):
            if sys.platform == "win32":
                yield
                return
            with open(self.root / "_locks" / f"{key}.lock", "w") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def _try_locked(self, key: str) -> Iterator[bool]:
        """Like _locked, but never waits: False when someone holds the repository."""
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            yield False
            return
        try:
            if sys.platform == "win32":
                yield True
                return
            with open(self.root / "_locks" / f"{key}.lock", "w") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            lock.release()

    # ── housekeeping ──────────────────────────────────────────────────────────

    @staticmethod
    def _whole(path: Path) -> bool:
        return (path / "HEAD").is_file() and (path / "objects").is_dir()

    def _touch(self, key: str) -> None:
        try:
            (self.root / f"{key}.used").touch()
        except OSError:
            pass

    def _used_at(self, mirror: Path) -> float:
        try:
            return (self.root / f"{mirror.stem}.used").stat().st_mtime
        except OSError:
            return mirror.stat().st_mtime

    def _evict(self, keep: str) -> None:
        """Remove the least recently used mirrors past max_repos, skipping any
        that is in use right now."""
        mirrors = [p for p in self.root.glob("*.git") if p.is_dir()]
        excess = len(mirrors) - self.max_repos
        if excess <= 0:
            return
        for mirror in sorted(mirrors, key=self._used_at):
            if excess <= 0:
                break
            key = mirror.stem
            if key == keep:
                continue
            with self._try_locked(key) as free:
                if not free:
                    continue
                self._rmtree(mirror)
                try:
                    (self.root / f"{key}.used").unlink()
                except OSError:
                    pass
                excess -= 1
                logger.info("git mirror evicted: %s", key)

    @staticmethod
    def _rmtree(path: Path) -> None:
        """Delete a directory. Git writes its objects read-only, which Windows
        will not delete until the flag is cleared."""
        def _writable_then_retry(func, failed_path, _exc):
            try:
                os.chmod(failed_path, stat.S_IWRITE)
                func(failed_path)
            except OSError:
                pass

        if path.exists():
            shutil.rmtree(str(path), onexc=_writable_then_retry)


def _verb(args: list[str]) -> str:
    """The git subcommand of *args*, for messages (not the options before it)."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--git-dir":
            skip = True
        elif not arg.startswith("-"):
            return arg
    return "command"


def remove_old_layout() -> int:
    """Delete the per-process temporary caches older versions left in the
    system temp directory (vt_gitcache_*). Returns how many were removed."""
    removed = 0
    now = time.time()
    for old in Path(tempfile.gettempdir()).glob(f"{_OLD_LAYOUT_PREFIX}*"):
        try:
            if old.is_dir() and now - old.stat().st_mtime > _OLD_LAYOUT_MAX_AGE_SECONDS:
                RepoMirrors._rmtree(old)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("removed %d old temporary git caches", removed)
    return removed


_default: Optional[RepoMirrors] = None
_default_lock = threading.Lock()


def get_default_mirrors() -> RepoMirrors:
    """The process's mirrors, under GIT_CACHE_DIR."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                remove_old_layout()
                _default = RepoMirrors()
    return _default
