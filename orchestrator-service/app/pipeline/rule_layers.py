"""
app/pipeline/rule_layers.py
───────────────────────────
The rule a sync runs with, built in layers (admin config, then the tenant's
config, then the repository's vartrack.json, then the admin's `force`):

  1. config.cue's rule for the platform and datasource     the bundle's
  2. the tenant's bundle.json rule for them (schema repo)  optional
  3. the repository's vartrack.json                        optional — repo_overrides.py
  4. the config.cue rule's `force`                         always last

Each layer overrides the one before it, key by key. branch_map,
file_path_map, variables_map and prune are merged key by key instead, and
prune.protection patterns add up. bundle.json and vartrack.json can "extends"
presets (app.pipeline.presets); those are resolved into their layer first.

Some keys only config.cue sets: the datasource's connection settings and credentials, and the rule's
secret_manager, repositories, exclude_repositories, overrides and self_heal
(rule.proto marks them admin_only).
The gateway and the watcher read those from config.cue, so a different value
further down would split the services. bundle.json values for them are
dropped with a warning; vartrack.json can only set the keys repo_overrides
allows. `force` cannot pin them either — they are set on the rule itself.

The rules are config.cue's: a bundle.json rule for a platform and datasource
config.cue has no rule for is ignored, since the gateway never routes to it.
A config.cue rule's `tenants` names the tenants whose bundle.json may layer it
(any tenant when empty): another tenant's bundle.json rule for it is ignored. A config.cue rule no allowed tenant's bundle.json names belongs to
its first tenant, or to tenant "default".

Without rules in config.cue (or without config.cue) there is one layer:
bundle.json's rules are used as they are.
"""
from __future__ import annotations

import copy
import functools
import logging
from typing import Any, Iterable, Optional

from app.pipeline.renamed_options import with_current_names
from app.pipeline.rule_options import ADMIN_ONLY, MERGEABLE  # noqa: F401 — read from rule.proto's annotations

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"

_IDENTITY = frozenset({"platform", "datasource"})

_warned: set[str] = set()


def _warn_once(message: str, *args: Any) -> None:
    text = message % args
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


@functools.cache
def admin_only_keys() -> frozenset[str]:
    """The keys only config.cue sets: the rule keys above and every sink
    setting a datasource block is translated to (mongo_uri, s3_region, …)."""
    from app.pipeline.datasource_settings import _MAPPINGS

    return ADMIN_ONLY | {key for mapping in _MAPPINGS.values() for key, _ in mapping}


def merge_child(parent: dict, child: dict) -> dict:
    """*child* laid over *parent*. A prune of
    true/false merges with a prune object as {enabled: …}, and the
    prune.protection patterns of both layers are kept: a layer can add keys
    that must never be deleted, not take them away."""
    out = {**parent, **child}
    for key in MERGEABLE:
        p, c = parent.get(key), child.get(key)
        if key == "prune" and isinstance(p, dict) != isinstance(c, dict) and p is not None and c is not None:
            p, c = _prune_object(p), _prune_object(c)
        if isinstance(p, dict) and isinstance(c, dict):
            merged = {**p, **c}
            if key == "prune" and isinstance(p.get("protection"), list) and isinstance(c.get("protection"), list):
                merged["protection"] = list(dict.fromkeys([*p["protection"], *c["protection"]]))
            out[key] = merged
    return out


def _prune_object(value: Any) -> dict:
    return value if isinstance(value, dict) else {"enabled": bool(value)}


def apply_force(rule: dict) -> dict:
    """*rule* with its `force` keys laid over it, last. Keys only config.cue
    sets are not forced: they are set on the rule itself."""
    force = rule.get("force")
    if not isinstance(force, dict) or not force:
        return rule
    blocked = admin_only_keys() | _IDENTITY
    ignored = sorted(k for k in force if k in blocked)
    if ignored:
        _warn_once("config.cue: the %s/%s rule forces %s, which only config.cue sets — set it on the rule itself",
                   rule.get("platform"), rule.get("datasource"), ", ".join(ignored))
    return {**rule, **{k: v for k, v in force.items() if k not in blocked}}


def _bundle() -> dict:
    from app.pipeline.datasource_settings import _bundle as load

    return load()


def cue_rules(bundle: Optional[dict] = None) -> list[dict]:
    """config.cue's rules."""
    bundle = _bundle() if bundle is None else bundle
    rules = [r for r in (bundle or {}).get("rules") or [] if isinstance(r, dict)]
    seen: set[tuple] = set()
    for rule in rules:
        key = (rule.get("platform"), rule.get("datasource"))
        if key in seen:
            # The gateway refuses to start on this; every lookup here takes the first.
            _warn_once("config.cue has more than one rule for %s/%s — only the first is used", *key)
        seen.add(key)
    return rules


def _find(rules: Iterable[dict], platform: Any, datasource: Any) -> Optional[dict]:
    return next((r for r in rules if r.get("platform") == platform and r.get("datasource") == datasource), None)


def _repo_url(bundle: dict, platform: str, repositories: Any) -> str:
    """The clone URL of the rule's repository on its platform, when it names
    exactly one ("org/repo", no wildcards)."""
    if not isinstance(repositories, list) or len(repositories) != 1:
        return ""
    repo = str(repositories[0]).strip("/")
    if not repo or any(c in repo for c in "*?[") or "/" not in repo:
        return ""
    for wrapper in bundle.get("platforms") or []:
        for kind, cfg in (wrapper or {}).items():
            if not isinstance(cfg, dict):
                continue
            name = f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind
            if name != platform:
                continue
            endpoint = str(cfg.get("endpoint") or ("https://github.com" if kind == "github" else "")).rstrip("/")
            return f"{endpoint}/{repo}.git" if endpoint else ""
    return ""


def _base(cue_rule: dict, bundle: dict) -> dict:
    rule = copy.deepcopy(with_current_names(cue_rule, "config.cue"))
    url = _repo_url(bundle, str(rule.get("platform", "")), rule.get("repositories"))
    if url:
        rule["repo_url"] = url
    return rule


def _without_admin_only(rule: dict, base: dict, source: str) -> dict:
    admin = admin_only_keys()
    dropped = sorted(k for k in rule if k in admin and rule[k] != base.get(k))
    if dropped:
        _warn_once("%s: the %s/%s rule sets %s, which only config.cue sets — ignored",
                   source, rule.get("platform"), rule.get("datasource"), ", ".join(dropped))
    return {k: v for k, v in rule.items() if k not in admin}


def layered_rule(cue_rule: dict, bundle_rule: Optional[dict], tenant_id: str,
                 bundle: Optional[dict] = None, source: str = "bundle.json") -> dict:
    """config.cue's *cue_rule* with *bundle_rule* (bundle.json's, or None)
    laid over it, then its `force`."""
    bundle = _bundle() if bundle is None else bundle
    base = _base(cue_rule, bundle)
    child = _without_admin_only(dict(with_current_names(bundle_rule, source)), base, source) if bundle_rule else {}
    rule = merge_child(base, child)
    rule.setdefault("tenant_id", tenant_id)
    return apply_force(rule)


def _allowed(cue: dict, tenant_id: str) -> bool:
    """Whether *tenant_id*'s bundle.json may layer config.cue's rule *cue*."""
    tenants = cue.get("tenants") or []
    return not tenants or tenant_id in tenants


def owner(cue: dict) -> str:
    """The tenant a config.cue rule belongs to when no bundle.json names it."""
    return next(iter(cue.get("tenants") or []), DEFAULT_TENANT)


def owners() -> list[str]:
    """The tenants config.cue's rules belong to when no bundle.json names them."""
    return list(dict.fromkeys(owner(c) for c in cue_rules()))


def _usable(cue: Optional[dict], bundle_rule: dict, tenant_id: str) -> bool:
    if cue is None:
        _warn_once("bundle.json of tenant %s: no rule in config.cue for %s/%s — ignored",
                   tenant_id, bundle_rule.get("platform"), bundle_rule.get("datasource"))
        return False
    if not _allowed(cue, tenant_id):
        _warn_once("bundle.json of tenant %s: the %s/%s rule belongs to %s (its tenants in config.cue) — ignored",
                   tenant_id, cue.get("platform"), cue.get("datasource"), ", ".join(cue["tenants"]))
        return False
    return True


def tenant_rules(bundle_rules: list[dict], tenant_id: str, claimed: set[tuple[Any, Any, Any]]) -> list[dict]:
    """The rules of *tenant_id*: its bundle.json rules, each over config.cue's
    rule for the same platform and datasource, and the config.cue rules it
    owns that no allowed tenant's bundle.json names (*claimed* holds the
    (tenant, platform, datasource) of every tenant's bundle.json rules)."""
    bundle = _bundle()
    cues = cue_rules(bundle)
    if not cues:
        return list(bundle_rules)
    out: list[dict] = []
    for rule in bundle_rules:
        cue = _find(cues, rule.get("platform"), rule.get("datasource"))
        if _usable(cue, rule, tenant_id):
            out.append(layered_rule(cue, rule, tenant_id, bundle))
    out.extend(layered_rule(cue, None, tenant_id, bundle)
               for cue in unclaimed(cues, claimed) if owner(cue) == tenant_id)
    return out


def _claimed(cue: dict, claimed: set[tuple[Any, Any, Any]]) -> bool:
    return any(p == cue.get("platform") and d == cue.get("datasource") and _allowed(cue, t)
               for t, p, d in claimed)


def unclaimed(cues: list[dict], claimed: set[tuple[Any, Any, Any]]) -> list[dict]:
    """The config.cue rules no allowed tenant's bundle.json names."""
    return [c for c in cues if not _claimed(c, claimed)]


def rule_for(platform: str, datasource: str, bundle_rule: Optional[dict], tenant_id: str,
             claimed: set[tuple[Any, Any, Any]]) -> Optional[dict]:
    """The rule for *platform* and *datasource* in *tenant_id*, from its
    bundle.json rule (or None) and config.cue's."""
    bundle = _bundle()
    cues = cue_rules(bundle)
    if not cues:
        return bundle_rule
    cue = _find(cues, platform, datasource)
    if bundle_rule is not None and not _usable(cue, bundle_rule, tenant_id):
        return None
    if cue is None:
        return None
    if bundle_rule is None and (tenant_id != owner(cue) or _claimed(cue, claimed)):
        return None
    return layered_rule(cue, bundle_rule, tenant_id, bundle)
