"""
app/pipeline/env_resolver.py
──────────────────────────────
Centralised environment resolution.

Priority order (first match wins):
  1. branch_map[branch]          explicit mapping
  2. file_path_map[file_path]    file-path exact match
  3. env_as_branch               branch name IS the env
  4. env_as_pr                   "pr-{number}"
  5. env_as_tags                 tag name IS the env
  6. "default"                   hardcoded fallback

env_as_pr — how pr_number is resolved
──────────────────────────────────────
PR webhook events (opened, synchronize, reopened) carry pr_number in the
payload and also call pr_branch_cache.register(branch, pr_number).

Plain branch push events do NOT carry pr_number.  But if the branch
belongs to an open PR, we still want env = "pr-{number}".

The fix: when env_as_pr is true and pr_number is None (plain push), we
consult the PRBranchCache.  If the branch is registered there, we get the
PR number and produce the correct env.  If not, we fall through normally.

Flow for a push to "feature/login" (branch has open PR #42):

  PR opened event (earlier):
    webhook_router.handle_pr_event()
      → pr_branch_cache.register("feature/login", "42")

  Push event (now):
    env_resolver.resolve(
        branch="feature/login",
        pr_number=None,           ← not in push payload
        env_as_pr=True,
    )
      → pr_number is None
      → check cache: lookup("feature/login") → "42"
      → env = "pr-42"            ✓  sink updates the PR environment

  PR merged (later):
    webhook_router.handle_pr_event()
      → pr_branch_cache.evict("feature/login")

  Push to feature/login after merge:
    → lookup("feature/login") → None
    → falls through to env_as_branch / "default"
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve(
    branch: str | None = None,
    file_path: str | None = None,
    pr_number: str | None = None,
    tag: str | None = None,
    branch_map: dict[str, str] | None = None,
    file_path_map: dict[str, str] | None = None,
    env_as_branch: bool = False,
    env_as_pr: bool = False,
    env_as_tags: bool = False,
) -> str:
    """
    Resolve the environment string from git event metadata and rule config.

    Returns
    -------
    str — never empty, falls back to "default".
    """
    branch_map    = branch_map    or {}
    file_path_map = file_path_map or {}

    # 1. Explicit branch → env mapping
    if branch and branch in branch_map:
        env = branch_map[branch]
        logger.debug("env_resolve via=branch_map branch=%s env=%s", branch, env)
        return env

    # 2. File-path → env mapping (exact match)
    if file_path and file_path in file_path_map:
        env = file_path_map[file_path]
        logger.debug("env_resolve via=file_path_map path=%s env=%s", file_path, env)
        return env

    # 3. env_as_branch — branch name itself is the env
    if env_as_branch and branch:
        logger.debug("env_resolve via=env_as_branch branch=%s", branch)
        return branch

    # 4. env_as_pr — "pr-{number}"
    #
    # Two sources for pr_number:
    #   a) Directly in the payload (PR webhook events: opened, synchronize, reopened)
    #   b) PRBranchCache lookup (plain push events to a branch that has an open PR)
    #
    # (b) is the key fix: without it, a plain push to a branch that already has
    # an open PR would fall through to "default" and miss the PR environment.
    if env_as_pr:
        effective_pr = pr_number

        if effective_pr is None and branch:
            # Plain push event — check if this branch belongs to an open PR.
            from app.pipeline.pr_branch_cache import get_cache
            effective_pr = get_cache().lookup(branch)
            if effective_pr:
                logger.debug(
                    "env_resolve via=pr_cache branch=%s pr=%s",
                    branch, effective_pr,
                )

        if effective_pr:
            env = f"pr-{effective_pr}"
            logger.debug("env_resolve via=env_as_pr pr=%s env=%s", effective_pr, env)
            return env

    # 5. env_as_tags — tag name is the env
    if env_as_tags and tag:
        env = tag.removeprefix("refs/tags/")
        logger.debug("env_resolve via=env_as_tags tag=%s env=%s", tag, env)
        return env

    # 6. Default fallback
    logger.debug("env_resolve via=default branch=%s", branch)
    return "default"
