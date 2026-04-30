"""
app/pipeline/repo_overrides.py
──────────────────────────────
Fetch and apply per-repo overrides from a vartrack.json file in the repository.

When a repository contains a vartrack.json file at its root, its settings
override matching keys from the central CUE bundle rule_config.  This lets
individual repos customise ETL behaviour (file paths, branch maps, sync mode,
etc.) without requiring central config changes.

Security model
──────────────
Only a safe-listed subset of rule_config keys can be overridden.  Infrastructure
keys (platform, datasource, repo_url, tenant_id, token, secrets, repositories,
self_heal, and Celery wiring) are always taken from the central bundle and
cannot be changed by the repo.

Schema registry
───────────────
vartrack.json has NO effect on the CUE schema registry.  Schema validation is
controlled entirely by the central bundle's schema_registry configuration.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.pipeline.models import PayloadContext

logger = logging.getLogger(__name__)

VARTRACK_JSON = "vartrack.json"

# Keys that a repo is allowed to override via vartrack.json.
# Infrastructure and security-critical keys are intentionally excluded.
_OVERRIDABLE_KEYS: frozenset[str] = frozenset({
    "apply_strategy",
    "branch_map",
    "dry_run",
    "env_as_branch",
    "env_as_pr",
    "env_as_tags",
    "file_name",
    "file_path_map",
    "prune",
    "root_key",
    "strict_validation",
    "sync_mode",
    "variables_map",
})


def apply_repo_overrides(ctx: "PayloadContext") -> "PayloadContext":
    """
    Fetch vartrack.json from the repository and merge its settings into
    ctx.rule_config, returning an updated PayloadContext.

    Returns the original ctx unchanged if:
    - vartrack.json does not exist in the repo at the current ref
    - vartrack.json cannot be parsed as a JSON object
    - vartrack.json contains no overridable keys

    Never raises — all errors are logged and the original ctx is returned.
    """
    from app.pipeline.git_extractor import GitExtractor

    token = ctx.rule_config.get("token")
    try:
        content = GitExtractor().extract_file(
            ctx.repo_url,
            ctx.ref,
            VARTRACK_JSON,
            token=token,
        )
    except Exception:
        logger.warning(
            "repo_overrides: failed to fetch %s repo=%s ref=%s",
            VARTRACK_JSON, ctx.repo_url, ctx.ref,
        )
        return ctx

    if content is None:
        logger.debug(
            "repo_overrides: no %s repo=%s ref=%s",
            VARTRACK_JSON, ctx.repo_url, ctx.ref,
        )
        return ctx

    try:
        overrides = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(
            "repo_overrides: invalid JSON in %s repo=%s ref=%s",
            VARTRACK_JSON, ctx.repo_url, ctx.ref,
        )
        return ctx

    if not isinstance(overrides, dict):
        logger.warning(
            "repo_overrides: %s must be a JSON object, got %s repo=%s",
            VARTRACK_JSON, type(overrides).__name__, ctx.repo_url,
        )
        return ctx

    applicable = {k: v for k, v in overrides.items() if k in _OVERRIDABLE_KEYS}
    blocked    = set(overrides) - _OVERRIDABLE_KEYS
    if blocked:
        logger.info(
            "repo_overrides: ignoring non-overridable keys %s repo=%s",
            sorted(blocked), ctx.repo_url,
        )

    if not applicable:
        return ctx

    logger.info(
        "repo_overrides: applying %s from %s repo=%s ref=%s",
        sorted(applicable), VARTRACK_JSON, ctx.repo_url, ctx.ref,
    )
    return replace(ctx, rule_config={**ctx.rule_config, **applicable})
