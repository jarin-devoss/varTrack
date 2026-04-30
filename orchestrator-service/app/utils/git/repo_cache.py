# app/utils/git/repo_cache.py
"""
Git repository cache with a bounded LRU map (max 20 entries).

Design:
  - Each entry maps  (repo_url, ref)  →  local_checkout_path
  - LRU eviction: oldest-accessed entry is removed when the cap is reached
  - PR branches live alongside regular branches; both are keyed by their
    full ref string (e.g. "refs/pull/42/head" or "refs/heads/main")
  - Thread-safe: protected by a threading.Lock
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import git  # GitPython

logger = logging.getLogger(__name__)


def _mask_url(url: str) -> str:
    """Replace any credentials embedded in a URL with '***' before logging."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            masked = parsed._replace(netloc=f"***@{host}")
            return urllib.parse.urlunparse(masked)
    except Exception:
        pass
    return url

_MAX_CACHE_SIZE = 20


@dataclass
class CacheEntry:
    """A single cached checkout."""
    repo_url: str
    ref: str
    path: Path
    commit_sha: str


class BranchRepoCache:
    """
    LRU cache for git checkouts.

    Bounded at MAX_CACHE_SIZE=20 entries to avoid disk exhaustion on
    workers processing many active PRs simultaneously.

    Usage::

        cache = BranchRepoCache()
        path, sha = cache.get_or_clone("https://github.com/org/repo", "refs/heads/main")
        # path is a read-only checkout – do not modify in place
    """

    def __init__(self, max_size: int = _MAX_CACHE_SIZE, base_dir: Optional[str] = None) -> None:
        self._max = max_size
        self._base = Path(base_dir or tempfile.mkdtemp(prefix="vt_gitcache_"))
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_or_clone(self, repo_url: str, ref: str) -> tuple[Path, str]:
        """
        Return a (path, commit_sha) for the requested ref.

        If the ref is already cached (and has not changed remotely), the
        cached path is returned without a network round-trip.  Otherwise
        a fresh clone/fetch is performed.
        """
        cache_key = self._key(repo_url, ref)

        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                # Promote to most-recently-used.
                self._cache.move_to_end(cache_key)
                # Quick check: does remote HEAD match cached SHA?
                try:
                    remote_sha = self._remote_sha(entry.path, ref)
                    if remote_sha == entry.commit_sha:
                        logger.debug("git cache hit: [key=%s]@%s", cache_key, ref)
                        return entry.path, entry.commit_sha
                    # Commit changed – pull and update.
                    logger.debug("git cache stale: [key=%s]@%s, pulling", cache_key, ref)
                    commit_sha = self._pull(entry.path, ref)
                    entry.commit_sha = commit_sha
                    return entry.path, commit_sha
                except Exception as exc:
                    logger.warning("git cache check failed (%s), re-cloning: %s", cache_key, exc)
                    self._evict_key(cache_key)

            # Cache miss or failed recovery – do a fresh clone.
            self._evict_lru_if_full()
            path, commit_sha = self._clone(repo_url, ref, cache_key)
            entry = CacheEntry(repo_url=repo_url, ref=ref, path=path, commit_sha=commit_sha)
            self._cache[cache_key] = entry
            logger.info("git cache: cloned [key=%s]@%s → %s", cache_key, ref, path)
            return path, commit_sha

    def invalidate(self, repo_url: str, ref: str) -> None:
        """Force-evict a specific (repo_url, ref) entry."""
        with self._lock:
            self._evict_key(self._key(repo_url, ref))

    def clear(self) -> None:
        """Remove all entries and their on-disk checkouts."""
        with self._lock:
            for entry in self._cache.values():
                self._rmtree(entry.path)
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _key(repo_url: str, ref: str) -> str:
        return hashlib.sha256(f"{repo_url}|{ref}".encode()).hexdigest()[:16]

    _GIT_ENV: dict[str, str] = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": (
            "ssh -o BatchMode=yes "
            "-o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=10"
        ),
    }

    def _clone(self, repo_url: str, ref: str, cache_key: str) -> tuple[Path, str]:
        dest = self._base / cache_key
        dest.mkdir(parents=True, exist_ok=True)

        # Shallow clone of the specific ref.
        repo = git.Repo.clone_from(
            repo_url,
            str(dest),
            depth=1,
            single_branch=True,
            branch=self._refspec_to_branch(ref),
            no_checkout=False,
            env=self._GIT_ENV,
        )
        commit_sha: str = repo.head.commit.hexsha
        return dest, commit_sha

    def _pull(self, path: Path, ref: str) -> str:
        repo = git.Repo(str(path))
        origin = repo.remote("origin")
        origin.pull(self._refspec_to_branch(ref), depth=1, env=self._GIT_ENV)
        commit_sha: str = repo.head.commit.hexsha
        return commit_sha

    def _remote_sha(self, path: Path, ref: str) -> str:
        """Non-destructive ls-remote to check remote HEAD without fetching."""
        repo = git.Repo(str(path))
        results = repo.git.ls_remote("origin", ref)
        if not results:
            raise ValueError(f"ref {ref!r} not found on remote")
        return results.split()[0]

    def _evict_lru_if_full(self) -> None:
        if len(self._cache) >= self._max:
            oldest_key, oldest_entry = next(iter(self._cache.items()))
            logger.debug("git cache evict LRU: %s", oldest_key)
            self._rmtree(oldest_entry.path)
            del self._cache[oldest_key]

    def _evict_key(self, key: str) -> None:
        if key in self._cache:
            self._rmtree(self._cache[key].path)
            del self._cache[key]

    @staticmethod
    def _refspec_to_branch(ref: str) -> str:
        """Convert a full refspec to a short branch / tag name for git clone --branch."""
        for prefix in ("refs/heads/", "refs/tags/", "refs/pull/"):
            if ref.startswith(prefix):
                return ref[len(prefix):]
        return ref  # already short

    @staticmethod
    def _rmtree(path: Path) -> None:
        try:
            shutil.rmtree(str(path), ignore_errors=True)
        except Exception:
            pass


# Module-level singleton used by pipeline tasks.
_default_cache: Optional[BranchRepoCache] = None
_cache_lock = threading.Lock()


def get_default_cache() -> BranchRepoCache:
    global _default_cache
    if _default_cache is None:
        with _cache_lock:
            if _default_cache is None:
                _default_cache = BranchRepoCache()
    return _default_cache
