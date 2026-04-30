"""
app/pipeline/pr_branch_cache.py
─────────────────────────────────
In-memory LRU cache: branch name → PR number.

Why this exists
───────────────
When someone pushes a commit to a branch that already has an open PR, the
webhook arrives as a plain push event — not a PR event.  The payload has no
pr_number field, so env_resolver.resolve() falls through to "default" instead
of producing "pr-42".

This cache bridges that gap:
  - PR open / PR sync events   → register(branch, pr_number)
  - PR close / PR merge events → evict(branch)
  - Every push event           → lookup(branch) → pr_number or None

Then env_resolver gets the pr_number even for plain branch pushes.

Design
──────
  - Capacity: 20 entries (configurable).  LRU eviction keeps the most
    recently active PRs hot and automatically drops stale ones.
  - Thread-safe: a single lock guards all mutations.
  - Process-local: lives in the Celery worker process.  Each worker maintains
    its own cache.  This is fine — a miss just falls back to "default" for
    that one task, which is the same behaviour as before this cache existed.
    No distributed coordination needed.
  - No persistence: cache is rebuilt from incoming webhook events.  On worker
    restart all entries start empty; PR events refill them as they arrive.

Typical entry lifetime
──────────────────────
  PR #42 opened  (branch: feature/login)
    → register("feature/login", "42")

  Push to feature/login
    → lookup("feature/login") = "42"  → env = "pr-42"  ✓

  Another push to feature/login
    → same lookup, still "pr-42"  ✓

  PR #42 merged / closed
    → evict("feature/login")

  Push to feature/login after merge
    → lookup("feature/login") = None  → falls back to env_as_branch or "default"
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

_DEFAULT_CAPACITY = 20


class PRBranchCache:
    """
    Thread-safe LRU cache mapping branch name → PR number (as string).

    Parameters
    ----------
    capacity : int
        Maximum number of branch → PR entries to hold.
        When full, the least-recently-used entry is evicted.
    """

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        self._capacity   = capacity
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._lock       = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def register(self, branch: str, pr_number: str | int) -> None:
        """
        Record that *branch* is the head branch of PR *pr_number*.
        Called when a PR is opened or synchronised (new commit pushed via PR UI).

        Parameters
        ----------
        branch    : e.g. "feature/login"
        pr_number : e.g. "42" or 42
        """
        if not branch or pr_number is None:
            return

        pr_str = str(pr_number)
        with self._lock:
            # Move to end (most recently used) or insert.
            if branch in self._cache:
                self._cache.move_to_end(branch)
            else:
                if len(self._cache) >= self._capacity:
                    evicted_branch, evicted_pr = self._cache.popitem(last=False)
                    logger.debug(
                        "pr_cache evict branch=%s pr=%s (capacity=%d)",
                        evicted_branch, evicted_pr, self._capacity,
                    )
            self._cache[branch] = pr_str
            logger.debug("pr_cache register branch=%s pr=%s", branch, pr_str)

    def evict(self, branch: str) -> None:
        """
        Remove the entry for *branch* (call on PR close / merge).

        Parameters
        ----------
        branch : e.g. "feature/login"
        """
        with self._lock:
            pr = self._cache.pop(branch, None)
            if pr is not None:
                logger.debug("pr_cache evict branch=%s pr=%s (closed)", branch, pr)

    def lookup(self, branch: str | None) -> str | None:
        """
        Return the PR number string for *branch*, or None if not cached.

        Accessing a cached entry promotes it to most-recently-used.

        Parameters
        ----------
        branch : branch name from the push event, e.g. "feature/login"

        Returns
        -------
        str | None — e.g. "42", or None if branch is not in an open PR.
        """
        if not branch:
            return None
        with self._lock:
            pr = self._cache.get(branch)
            if pr is not None:
                self._cache.move_to_end(branch)
                logger.debug("pr_cache hit branch=%s pr=%s", branch, pr)
            else:
                logger.debug("pr_cache miss branch=%s", branch)
            return pr

    # ── Inspection helpers ────────────────────────────────────────────────────

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the current cache contents (for debug/metrics)."""
        with self._lock:
            return dict(self._cache)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            logger.debug("pr_cache cleared")


# ── Process-level singleton ───────────────────────────────────────────────────
# Imported and used by both the webhook router (to register/evict) and
# env_resolver (to look up pr_number for plain branch pushes).

_cache = PRBranchCache(capacity=_DEFAULT_CAPACITY)


def get_cache() -> PRBranchCache:
    """Return the process-level singleton cache."""
    return _cache
