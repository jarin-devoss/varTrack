"""
app/pipeline/repo_overrides.py
──────────────────────────────
Fetch and apply per-repo overrides from a vartrack.json file in the repository.

When a repository contains a vartrack.json file — at its root, or under
.github/, .gitea/ or .gitlab/: the first of VARTRACK_JSON_NAMES found at the
pushed ref — its settings override matching keys of the rule — config.cue's
rule with the tenant's bundle.json rule over it (rule_layers.py). branch_map,
file_path_map, variables_map and prune are merged key by key; the rule's
`force` keys in config.cue are applied after vartrack.json, so they win.
This lets individual repos customise ETL behaviour (file paths, branch maps,
sync mode, etc.) without requiring central config changes. A vartrack.json
can "extends" presets (app.pipeline.presets); "local>" is the repo's own Git
server.

Security model
──────────────
Only a safe-listed subset of rule_config keys can be overridden.  Infrastructure
keys (platform, datasource, repo_url, token, secrets, repositories,
self_heal, and Celery wiring) are always taken from the central bundle and
cannot be changed by the repo. The tenant is not a key at all: it is the one that
owns the pushed repository, and a tenant_id in vartrack.json is dropped with a warning.

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

from app.pipeline.renamed_options import with_current_names
from app.pipeline.rule_options import REPO_OVERRIDABLE

if TYPE_CHECKING:
    from app.pipeline.models import PayloadContext

logger = logging.getLogger(__name__)

VARTRACK_JSON = "vartrack.json"

# Where a repository's overrides may live, looked up in this order — the
# first one found is used.
VARTRACK_JSON_NAMES: tuple[str, ...] = (
    VARTRACK_JSON,
    ".github/vartrack.json",
    ".gitea/vartrack.json",
    ".gitlab/vartrack.json",
)

# Keys that a repo is allowed to override via vartrack.json: the options
# rule.proto marks repo_overridable. Infrastructure and security-critical keys
# are intentionally not marked.
_OVERRIDABLE_KEYS: frozenset[str] = REPO_OVERRIDABLE


class RepoConfigError(ValueError):
    """A vartrack.json that is there but cannot be used: not JSON, not an
    object, naming a preset that cannot be read, or setting a sync_mode that is not a
    mode. The sync stops — it does not go ahead without settings the repository asked
    for (a branch_map, say)."""


def apply_repo_overrides(ctx: "PayloadContext", tenant_id: str | None = None) -> "PayloadContext":
    """
    Fetch vartrack.json from the repository and merge its settings into
    ctx.rule_config, returning an updated PayloadContext.

    Returns the original ctx unchanged if:
    - vartrack.json does not exist in the repo at the current ref, or
      cannot be fetched
    - vartrack.json contains no overridable keys

    Raises RepoConfigError when vartrack.json is not a JSON object, or a
    preset it extends cannot be resolved, or comes from a repository that is not
    one of *tenant_id*'s preset sources (presets.Scope).
    """
    from app.pipeline.git_extractor import GitExtractor

    from app.dependencies import SourceUnavailable
    from app.pipeline.git_credentials import credential_for

    token = credential_for(ctx.rule_config, ctx.repo_url)
    content, name = None, VARTRACK_JSON
    try:
        extractor = GitExtractor()
        for name in VARTRACK_JSON_NAMES:
            content = extractor.extract_file(
                ctx.repo_url, ctx.ref, name, token=token, revision=ctx.commit_sha,
            )
            if content is not None:
                break
    except SourceUnavailable:
        # The repository cannot be read: the sync could not read its files either, and a
        # repository's own settings are not to be skipped because they could not be fetched.
        raise
    except Exception as exc:
        raise RepoConfigError(f"{name} in {ctx.repo_url} could not be fetched: {exc}") from exc

    if content is None:
        logger.debug(
            "repo_overrides: no %s repo=%s ref=%s",
            VARTRACK_JSON, ctx.repo_url, ctx.ref,
        )
        return ctx

    try:
        overrides = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RepoConfigError(f"{name} in {ctx.repo_url} is not valid JSON: {exc}") from exc

    if not isinstance(overrides, dict):
        raise RepoConfigError(
            f"{name} in {ctx.repo_url} must be a JSON object, not {type(overrides).__name__}"
        )

    if overrides.get("extends"):
        # Presets: "local>" is the repo's own Git server.
        from app.pipeline.presets import Origin, PresetError, Resolver, scope_of
        try:
            scope = scope_of(tenant_id or ctx.rule_config.get("tenant_id"))
            overrides = Resolver(Origin.of(ctx.repo_url, token), scope=scope).resolve(overrides)
        except PresetError as exc:
            raise RepoConfigError(f"{name} in {ctx.repo_url}: {exc}") from exc

    overrides = with_current_names(overrides, f"{name} in {ctx.repo_url}")
    from app.utils.enums.sync_mode import sync_mode_problem
    if "sync_mode" in overrides and (problem := sync_mode_problem(overrides["sync_mode"])):
        # Not read as AUTO: the repository asked for a way of writing that does not exist.
        raise RepoConfigError(f"{name} in {ctx.repo_url}: {problem}")
    from app.pipeline.rule_layers import warn_tenant_claim
    warn_tenant_claim(overrides, f"{name} in {ctx.repo_url}", "the one that owns the pushed repository")
    applicable = {k: v for k, v in overrides.items() if k in _OVERRIDABLE_KEYS}
    blocked    = set(overrides) - _OVERRIDABLE_KEYS - {"tenant_id"}   # that one was warned about above
    if blocked:
        logger.info(
            "repo_overrides: ignoring non-overridable keys %s repo=%s",
            sorted(blocked), ctx.repo_url,
        )

    if not applicable:
        return ctx

    logger.info(
        "repo_overrides: applying %s from %s repo=%s ref=%s",
        sorted(applicable), name, ctx.repo_url, ctx.ref,
    )
    from app.pipeline.rule_layers import apply_force, merge_child
    return replace(ctx, rule_config=apply_force(merge_child(ctx.rule_config, applicable)))
