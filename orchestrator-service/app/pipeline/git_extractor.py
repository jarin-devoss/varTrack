"""
app/pipeline/git_extractor.py
──────────────────────────────
Reads file content from git, at an exact commit.

Architecture:
  - RepoMirrors     → one bare mirror per repository on disk
                      (app/utils/git/mirror.py)
  - GitExtractor    → reads files from a mirror by commit, exposing the API
                      that file_collector.py and sync_all_task need

Key methods:

  extract_file(repo_url, ref, file_path, *, token, revision)
      Fetch a single file's content as a string.  Returns None if the file
      doesn't exist at that commit.  Used by Strategy 1 and 2 in file_collector.

  extract_changed_files(repo_url, ref, filenames, *, token, revision)
      Fetch a list of specific files and return FileContent namedtuples.
      Used by Strategy 3 (fetch all files touched by the push).

  get_commit_sha(repo_url, branch, *, token)
      Return the current HEAD SHA for a branch without cloning anything.
      Used by sync_all_task to synthesise a push payload.

Which commit is read:
  A sync is recorded under the commit that was pushed, so it reads that
  commit: pass it as *revision*.  Without one (a branch or tag asked for by
  name: the web UI, the CLI, presets) *ref* is resolved to a commit first.
  Either way the commit is fetched into the mirror if it is not there yet, and
  the file is read from the commit, never from a branch that may have moved.

Path traversal protection:
  A path that is absolute, has a backslash or climbs with '..' is refused
  (clean_path); git is then asked for that path inside the commit.

Token handling:
  Credentials must never appear in log messages or exception strings.
  - the token goes to git through its environment (mirror.py), never in the
    URL or on the command line, and is scrubbed from git's error text
  - All log calls use the repo_url given, which carries no token
"""
from __future__ import annotations

import logging
import time
from typing import NamedTuple, Optional

from app.utils.git.mirror import RepoMirrors, clean_path, get_default_mirrors, is_commit_sha

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


def _decode(raw: bytes) -> str:
    """
    Decode file bytes, detecting BOM-prefixed encodings first.

    Inspects the BOM to pick the correct codec (UTF-32, UTF-16, UTF-8 with BOM,
    or plain UTF-8), then decodes.  Only falls back to errors='replace' for
    genuinely unknown-encoding files.
    """
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
    Read files from git repositories, at a commit, through a bare mirror.

    Usage::

        extractor = GitExtractor()
        content = extractor.extract_file(
            "https://github.com/acme/config",
            "refs/heads/main",
            "services/payments/config.yaml",
            revision="a1b2c3…",       # the commit that was pushed
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
        revision: Optional[str] = None,
    ) -> Optional[str]:
        """
        Return the content of *file_path* at a commit as a UTF-8 string.

        Returns None if the file doesn't exist at that commit (deleted, never
        committed), or if the repository or the ref can't be reached.

        Parameters
        ----------
        repo_url  : clone URL, without credentials
        ref       : full git ref ("refs/heads/main", "refs/tags/v1") or a short
                    branch or tag name; resolved to a commit unless *revision*
                    is given
        file_path : relative path inside the repo, e.g. "config/prod.yaml"
        token     : optional OAuth2 / PAT for private repos
        revision  : the commit to read (a full commit id), when it is known
        """
        _t0 = time.perf_counter()
        try:
            clean_path(file_path)
        except ValueError:
            logger.warning(
                "git_extractor: path traversal attempt path=%r repo=%s",
                file_path, repo_url,
            )
            return None
        try:
            mirrors, sha = self._commit(repo_url, ref, token, revision)
        except Exception:
            logger.exception("git_extractor: clone/fetch failed repo=%s ref=%s", repo_url, ref)
            _obs_git("error", time.perf_counter() - _t0)
            return None

        try:
            raw = mirrors.read(repo_url, sha, file_path)
        except Exception:
            # Raise instead of returning None: None is indistinguishable from
            # "file was deleted", which (with prune enabled) would cause all keys
            # from this file to be pruned even though the file exists but could
            # not be read.  Raising lets file_collector skip ETL safely.
            logger.exception("git_extractor: read failed path=%s repo=%s", file_path, repo_url)
            raise

        if raw is None:
            logger.debug(
                "git_extractor: file not found path=%s repo=%s commit=%s",
                file_path, repo_url, sha,
            )
            return None
        content = _decode(raw)
        logger.debug(
            "git_extractor: fetched path=%s sha=%s bytes=%d",
            file_path, sha, len(raw),
        )
        _obs_git("ok", time.perf_counter() - _t0)
        return content

    def extract_changed_files(
        self,
        repo_url: str,
        ref: str,
        filenames: list[str],
        *,
        token: Optional[str] = None,
        revision: Optional[str] = None,
    ) -> list[FileContent]:
        """
        Fetch a specific list of files at one commit.

        Returns a list of FileContent(path, content) where content is None for
        files that were deleted or couldn't be read.

        Parameters
        ----------
        repo_url  : clone URL, without credentials
        ref       : full git ref; resolved to a commit unless *revision* is given
        filenames : list of repo-relative file paths to fetch
        token     : optional OAuth2 / PAT
        revision  : the commit to read (a full commit id), when it is known
        """
        if not filenames:
            return []

        try:
            mirrors, sha = self._commit(repo_url, ref, token, revision)
        except Exception:
            logger.exception("git_extractor: clone/fetch failed repo=%s ref=%s", repo_url, ref)
            return [FileContent(path=fp, content=None) for fp in filenames]

        results: list[FileContent] = []
        for fp in filenames:
            try:
                raw = mirrors.read(repo_url, sha, fp)
            except ValueError:
                logger.warning(
                    "git_extractor: path traversal attempt path=%r repo=%s", fp, repo_url
                )
                results.append(FileContent(path=fp, content=None))
                continue
            except Exception:
                # Same as extract_file, but a batch carries on with the other
                # files; the None content tells the caller this one failed.
                logger.exception("git_extractor: read error path=%s", fp)
                results.append(FileContent(path=fp, content=None))
                continue

            if raw is None:
                logger.debug("git_extractor: file deleted/missing path=%s sha=%s", fp, sha)
                results.append(FileContent(path=fp, content=None))
                continue
            results.append(FileContent(path=fp, content=_decode(raw)))

        logger.debug(
            "git_extractor: fetched %d/%d files repo=%s ref=%s commit=%s",
            sum(1 for r in results if r.content is not None), len(filenames),
            repo_url, ref, sha,
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

        Used by sync_all_task and the web UI to synthesise a push payload.
        Asks the remote (`git ls-remote`): nothing is cloned or fetched.

        Parameters
        ----------
        repo_url : clone URL
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

        try:
            sha = get_default_mirrors().resolve(repo_url, token, ref)
            logger.debug("git_extractor: sha=%s repo=%s branch=%s", sha, repo_url, branch)
            if _rc is not None:
                _rc.set(_cache_key, sha, ttl=120)
            return sha
        except Exception:
            # raise from None keeps git's own error text, which names only the
            # token-free URL, out of the exception chain.
            raise RuntimeError(
                f"get_commit_sha failed for {repo_url}@{branch}"
            ) from None

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _commit(
        repo_url: str, ref: str, token: Optional[str], revision: Optional[str],
    ) -> tuple[RepoMirrors, str]:
        """The mirror of *repo_url* and the commit to read, which is in it: the
        revision when it is a commit id, else what *ref* points to now; fetched
        into the mirror when it is not there yet."""
        mirrors = get_default_mirrors()
        sha = revision if is_commit_sha(revision) else mirrors.resolve(repo_url, token, ref)
        mirrors.ensure(repo_url, token)
        if not mirrors.has_commit(repo_url, sha):
            mirrors.fetch(repo_url, token, sha=sha, ref=ref)
        return mirrors, sha
