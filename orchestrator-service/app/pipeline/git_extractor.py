"""
app/pipeline/git_extractor.py
──────────────────────────────
Thin façade over BranchRepoCache that fetches file content from git.

Architecture:
  - BranchRepoCache   → manages clones on disk (app/utils/git/repo_cache.py)
  - GitExtractor      → reads files from a cached checkout, exposing the API
                        that file_collector.py and sync_all_task need

Key methods:

  extract_file(repo_url, ref, file_path, *, token)
      Fetch a single file's content as a string.  Returns None if the file
      doesn't exist in the ref.  Used by Strategy 1 and 2 in file_collector.

  extract_changed_files(repo_url, ref, filenames, *, token)
      Fetch a list of specific files and return FileContent namedtuples.
      Used by Strategy 3 (fetch all files touched by the push).

  get_commit_sha(repo_url, branch, *, token)
      Return the current HEAD SHA for a branch without keeping a checkout.
      Used by sync_all_task to synthesise a push payload.

All methods delegate to BranchRepoCache.get_or_clone().

Path traversal protection:
  Path.resolve() + candidate.relative_to(base) prevents a caller-supplied
  path from escaping the checkout directory — raises ValueError if the
  resolved path is outside the base.

Token handling:
  Credentials must never appear in log messages or exception strings.
  - _inject_token() produces a URL with embedded credentials but is NEVER logged
  - All log calls use the original repo_url (without token)
  - Exceptions are re-raised from None to prevent exception chains from
    leaking the token-injected URL
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import NamedTuple, Optional

from app.utils.git.repo_cache import get_default_cache

logger = logging.getLogger(__name__)


def _obs_git(outcome: str, elapsed: float) -> None:
    """Fire-and-forget git fetch duration metric."""
    try:
        from app.monitoring import get_metrics
        m = get_metrics()
        if m:
            m.observe_git_fetch(outcome, elapsed)
    except Exception:
        pass


class FileContent(NamedTuple):
    """Result type for extract_changed_files."""
    path: str
    content: Optional[str]   # None if file was deleted or not found


def _read_file_content(full_path: Path) -> str:
    """
    Read a file and decode it, detecting BOM-prefixed encodings first.

    Reads raw bytes first, inspects the BOM to pick the correct codec
    (UTF-32, UTF-16, UTF-8 with BOM, or plain UTF-8), then decodes.
    Only falls back to errors='replace' for genuinely unknown-encoding files.
    """
    raw = full_path.read_bytes()
    # UTF-32 BOM must be checked before UTF-16 (its 4-byte BOM starts with the
    # same 2 bytes as the UTF-16 BOM).
    if raw.startswith((b'\x00\x00\xfe\xff', b'\xff\xfe\x00\x00')):
        return raw.decode('utf-32')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    # UTF-8 (with or without BOM)
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw[3:].decode('utf-8')
    return raw.decode('utf-8', errors='replace')


class GitExtractor:
    """
    Read files from git repositories via the process-level LRU clone cache.

    Usage::

        extractor = GitExtractor()
        content = extractor.extract_file(
            "https://github.com/acme/config",
            "refs/heads/main",
            "services/payments/config.yaml",
        )
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def extract_file(
        self,
        repo_url: str,
        ref: str,
        file_path: str,
        *,
        token: Optional[str] = None,
    ) -> Optional[str]:
        """
        Return the content of *file_path* at *ref* as a UTF-8 string.

        Returns None if the file doesn't exist in this ref (deleted, never
        committed, or ref not found).

        Parameters
        ----------
        repo_url  : HTTPS clone URL (token injected automatically if provided)
        ref       : full git ref, e.g. "refs/heads/main" or "refs/pull/42/head"
        file_path : relative path inside the repo, e.g. "config/prod.yaml"
        token     : optional OAuth2 / PAT for private repos
        """
        effective_url = _inject_token(repo_url, token)
        _t0 = time.perf_counter()
        try:
            checkout_path, sha = get_default_cache().get_or_clone(effective_url, ref)
        except Exception:
            # Log repo_url (no token), not effective_url (has token embedded)
            logger.exception(
                "git_extractor: clone/fetch failed repo=%s ref=%s", repo_url, ref
            )
            _obs_git("error", time.perf_counter() - _t0)
            return None

        try:
            full_path = _safe_join(Path(checkout_path), file_path)
        except ValueError:
            logger.warning(
                "git_extractor: path traversal attempt path=%r repo=%s",
                file_path, repo_url,
            )
            return None

        if not full_path.exists():
            logger.debug(
                "git_extractor: file not found path=%s repo=%s ref=%s",
                file_path, repo_url, ref,
            )
            return None

        try:
            content = _read_file_content(full_path)
            logger.debug(
                "git_extractor: fetched path=%s sha=%s bytes=%d",
                file_path, sha, len(content),
            )
            _obs_git("ok", time.perf_counter() - _t0)
            return content
        except OSError:
            # Raise instead of returning None: None is indistinguishable from
            # "file was deleted", which (with prune enabled) would cause all keys
            # from this file to be pruned even though the file exists but had a
            # transient I/O error.  Raising lets file_collector skip ETL safely.
            logger.exception(
                "git_extractor: read failed path=%s repo=%s", file_path, repo_url
            )
            raise

    def extract_changed_files(
        self,
        repo_url: str,
        ref: str,
        filenames: list[str],
        *,
        token: Optional[str] = None,
    ) -> list[FileContent]:
        """
        Fetch a specific list of files at *ref*.

        Returns a list of FileContent(path, content) where content is None for
        files that were deleted or couldn't be read.

        Parameters
        ----------
        repo_url  : HTTPS clone URL
        ref       : full git ref
        filenames : list of repo-relative file paths to fetch
        token     : optional OAuth2 / PAT
        """
        if not filenames:
            return []

        effective_url = _inject_token(repo_url, token)
        try:
            checkout_path, sha = get_default_cache().get_or_clone(effective_url, ref)
        except Exception:
            logger.exception(
                "git_extractor: clone/fetch failed repo=%s ref=%s", repo_url, ref
            )
            return [FileContent(path=fp, content=None) for fp in filenames]

        results: list[FileContent] = []
        for fp in filenames:
            try:
                full_path = _safe_join(Path(checkout_path), fp)
            except ValueError:
                logger.warning(
                    "git_extractor: path traversal attempt path=%r repo=%s", fp, repo_url
                )
                results.append(FileContent(path=fp, content=None))
                continue

            if not full_path.exists():
                logger.debug("git_extractor: file deleted/missing path=%s sha=%s", fp, sha)
                results.append(FileContent(path=fp, content=None))
                continue
            try:
                content = _read_file_content(full_path)
                results.append(FileContent(path=fp, content=content))
            except OSError:
                # Same as extract_file: raise instead of returning None so the
                # caller knows this was a read error, not a deletion event.
                logger.exception("git_extractor: read error path=%s", fp)
                results.append(FileContent(path=fp, content=None))
                # Note: for batch fetches we log and continue to process remaining
                # files; the None content tells the caller this file had an I/O error.

        logger.debug(
            "git_extractor: fetched %d/%d files repo=%s ref=%s",
            sum(1 for r in results if r.content is not None), len(filenames),
            repo_url, ref,
        )
        return results

    def get_commit_sha(
        self,
        repo_url: str,
        branch: str,
        *,
        token: Optional[str] = None,
    ) -> str:
        """
        Return the current HEAD commit SHA for *branch*.

        Used by sync_all_task to synthesise a synthetic push payload without
        requiring a full checkout — the cache will do a git ls-remote if the
        ref is already cached (fast path) or a shallow clone otherwise.

        Parameters
        ----------
        repo_url : HTTPS clone URL
        branch   : short branch name, e.g. "main"

        Returns
        -------
        str — 40-character hex SHA

        Raises
        ------
        RuntimeError — if the repo is unreachable or the branch doesn't exist
        """
        ref = f"refs/heads/{branch}"

        # Fast path: check Redis cache first (TTL=120s).
        # Keyed on repo_url (without token) so the cache is tenant-safe.
        from app.utils.redis_cache import get_cache as _get_cache
        import hashlib as _hashlib
        _cache_key = "sha:" + _hashlib.sha256(f"{repo_url}:{branch}".encode()).hexdigest()[:24]
        _rc = _get_cache()
        if _rc is not None:
            cached = _rc.get(_cache_key)
            if cached:
                logger.debug("git_extractor: sha cache hit repo=%s branch=%s sha=%s",
                             repo_url, branch, cached)
                return cached

        effective_url = _inject_token(repo_url, token)
        try:
            _, sha = get_default_cache().get_or_clone(effective_url, ref)
            logger.debug("git_extractor: sha=%s repo=%s branch=%s", sha, repo_url, branch)
            if _rc is not None:
                _rc.set(_cache_key, sha, ttl=120)
            return sha
        except Exception:
            # raise from None prevents the exception chain from leaking
            # effective_url which contains the embedded OAuth2 token.
            raise RuntimeError(
                f"get_commit_sha failed for {repo_url}@{branch}"
            ) from None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_join(base: Path, user_path: str) -> Path:
    """
    Safely join *user_path* under *base*, raising ValueError on path traversal.

    Resolves both paths to eliminate '..' components, then verifies the
    result is under (or equal to) the base directory.  Raises ValueError
    if the resolved path escapes the base.

    Prevents paths like "../../etc/passwd" from reading files outside the
    repository checkout.
    """
    candidate = (base / user_path).resolve()
    base_resolved = base.resolve()
    # relative_to() raises ValueError if candidate is not under base_resolved
    candidate.relative_to(base_resolved)
    return candidate


def _inject_token(repo_url: str, token: Optional[str]) -> str:
    """
    Embed an OAuth2 / PAT token into an HTTPS URL for private repo access.

    The returned URL MUST NOT be logged (it contains credentials).
    Log repo_url (the original, token-free URL) instead.

    String concatenation (not f-string) prevents the token from appearing
    in tracebacks or log output if the URL is part of an exception message.
    """
    if not token or not repo_url.startswith("https://"):
        return repo_url
    bare = repo_url[len("https://"):]
    return "https://oauth2:" + token + "@" + bare
