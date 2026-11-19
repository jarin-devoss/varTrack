"""
app/pipeline/write_order.py
────────────────────────────
An older commit never overwrites a newer one.

Two pushes to one branch make two syncs, and the target lock (stage_sync.target_locks) lets them
write one after the other, in the order they took the lock, not the order they were pushed. If the
older commit's sync takes the lock second, it writes the older values over the newer ones, and the
store stays wrong until the next push: Git is at the newer commit, the store at the older.

So each file written to a place has its commit remembered, here, and a sync that is about to write a file
asks first. It skips the file when the commit already written is a descendant of its own (its own is
older). It writes when it is the same commit, a descendant, or unrelated history (a force push), and when
nothing can say: no record, no Redis, or a commit that cannot be fetched. Skipping is only ever for a commit
that Git itself says is older.

The record is kept per file, not per environment: a push that touched one file did not write the others, and
a newer commit that did not write a file is no reason to skip an older one that did.
It is written and read inside the target lock, so the check and the write are one step as far as another
sync of the same place is concerned. It is written only once every file of the sync has been written: a sync
that fails and rolls back leaves the earlier record as it was.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

KEY_PREFIX = "vt:written-commit:"
TTL_SECONDS = 90 * 24 * 3600      # renewed by every write: a place nobody has written for 90 days is forgotten


def _key(datasource: str, place: str, repo_url: str, file_path: str) -> str:
    from app.ui import ledger
    return f"{KEY_PREFIX}{datasource}:{place}:{ledger.inventory_key(repo_url, file_path)}"


def _client() -> Any:
    from app.ui import ledger
    return ledger._redis()


def newer_commit_written(
    *, datasource: str, place: str, repo_url: str, file_path: str, commit_sha: str, rule_config: dict,
    client: Any = None,
) -> str | None:
    """The commit already written for this file at this place, when it is newer than *commit_sha*; else None
    (write)."""
    from app.utils.git.mirror import is_commit_sha

    if not is_commit_sha(commit_sha):
        return None            # a CLI push has a label, not a commit: there is nothing to order
    try:
        recorded = (client if client is not None else _client()).get(_key(datasource, place, repo_url, file_path))
    except Exception as exc:
        logger.debug("write order: the record of %s could not be read (%s); writing", file_path, exc)
        return None
    if not recorded or recorded == commit_sha or not is_commit_sha(recorded):
        return None
    try:
        from app.pipeline.git_credentials import credential_for
        from app.utils.git.mirror import get_default_mirrors

        mirrors = get_default_mirrors()
        if not mirrors.has_commit(repo_url, recorded):
            mirrors.fetch(repo_url, credential_for(rule_config, repo_url), sha=recorded)
        if mirrors.is_ancestor(repo_url, commit_sha, recorded):
            return recorded
    except Exception as exc:
        # Not knowing is not a reason to skip a write.
        logger.warning("write order: could not tell whether %s is older than %s for %s: %s; writing",
                       commit_sha[:12], recorded[:12], file_path, exc)
    return None


def record_written(
    *, datasource: str, place: str, repo_url: str, file_path: str, commit_sha: str, client: Any = None,
) -> None:
    """Remember that *commit_sha*'s version of the file is what the place holds now."""
    from app.utils.git.mirror import is_commit_sha

    if not is_commit_sha(commit_sha):
        return
    try:
        (client if client is not None else _client()).set(
            _key(datasource, place, repo_url, file_path), commit_sha, ex=TTL_SECONDS)
    except Exception as exc:
        logger.debug("write order: the commit written for %s could not be recorded (%s)", file_path, exc)
