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
prune.protection patterns add up. schema_map is a list that adds up too, config.cue's entries first (the first
entry that matches a file wins), so a path the operator mapped cannot be remapped by a tenant. bundle.json and vartrack.json can "extends"
presets (app.pipeline.presets); those are resolved into their layer first.

Some keys only config.cue sets: the datasource's connection settings and credentials, and the rule's
secret_manager, repositories, exclude_repositories, overrides and self_heal
(rule.proto's settable_in says CONFIG_CUE for them).
The gateway and the watcher read those from config.cue, so a different value
further down would split the services. bundle.json may set exactly the options
rule.proto declares settable there: a value for anything else is dropped with a
warning, whether it is reserved to config.cue or an option nobody declared, so
a key a sink happens to read cannot be set from a tenant's repository by
accident. vartrack.json can only set the keys repo_overrides allows. `force`
cannot pin them either — they are set on the rule itself.

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
import threading
from typing import Any, Iterable, Optional

from app.pipeline.renamed_options import with_current_names
from app.pipeline.rule_options import ADMIN_ONLY, MERGEABLE, TENANT_SETTABLE  # noqa: F401 — read from rule.proto's annotations

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
def config_cue_keys() -> frozenset[str]:
    """The keys only config.cue sets: the rule options reserved to it and every field of a
    datasource block (hosts, extra_flags, …), which a sink reads from that block alone."""
    from app.pipeline.rule_options import FIELDS
    from vartrack.v1.models import datasource_pb2

    fields = {f.name for kind in datasource_pb2.DataSource.DESCRIPTOR.fields if kind.message_type
              for f in kind.message_type.fields}
    return ADMIN_ONLY | (fields - set(FIELDS))


# Keys a bundle.json rule may carry that are not options: notes, and preset syntax
# (resolved before the rule is layered).
_NOT_SETTINGS = frozenset({"description", "$schema", "extends"})


def warn_tenant_claim(options: dict, where: str, owner: str, tenant: str = "") -> None:
    """Say, once, that a file wrote a tenant_id: the tenant is never something a file says, so it is dropped.
    *owner* says where the tenant really comes from; *tenant*, when the file belongs to one, lets the warning
    name a key that named another (which could not have acted as it)."""
    if "tenant_id" not in options:
        return
    named = options["tenant_id"]
    other = (f" (it names tenant {named}, but this file is tenant {tenant}'s)"
             if tenant and isinstance(named, str) and named and named != tenant else "")
    _warn_once("%s sets tenant_id, which no file may set — the tenant is %s; ignored%s", where, owner, other)


def tenant_settable(rule: dict) -> tuple[dict, list[str], list[str]]:
    """(the keys of *rule* a tenant's bundle.json may set, the ones config.cue
    reserves, the ones no option declares). Only the first are used."""
    kept, reserved, unknown = {}, [], []
    config_cue = config_cue_keys()
    for key, value in rule.items():
        if key in TENANT_SETTABLE:
            kept[key] = value
        elif key in config_cue:
            reserved.append(key)
        elif key not in _NOT_SETTINGS:
            unknown.append(key)
    return kept, sorted(reserved), sorted(unknown)


def merge_child(parent: dict, child: dict) -> dict:
    """*child* laid over *parent*. A prune of
    true/false merges with a prune object as {enabled: …}, and the
    prune.protection patterns of both layers are kept: a layer can add keys
    that must never be deleted, not take them away."""
    out = {**parent, **child}
    if parent.get("schema_map") or child.get("schema_map"):
        out["schema_map"] = [*(parent.get("schema_map") or []), *(child.get("schema_map") or [])]
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
    blocked = config_cue_keys() | _IDENTITY
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


def _settable_by_tenant(rule: dict, base: dict, source: str, tenant_id: str = "") -> dict:
    """The part of a tenant's *rule* that may be laid over config.cue's *base*."""
    kept, reserved, unknown = tenant_settable(rule)
    warn_tenant_claim(rule, f"{source}: the {rule.get('platform')}/{rule.get('datasource')} rule",
                      f"the one whose schema repository holds this file ({tenant_id})", tenant_id)
    unknown = [k for k in unknown if k != "tenant_id"]
    dropped = [k for k in reserved if rule[k] != base.get(k)]
    if dropped:
        _warn_once("%s: the %s/%s rule sets %s, which only config.cue sets — ignored",
                   source, rule.get("platform"), rule.get("datasource"), ", ".join(dropped))
    if unknown:
        _warn_once("%s: the %s/%s rule sets %s, which %s not %s a tenant may set — ignored",
                   source, rule.get("platform"), rule.get("datasource"), ", ".join(unknown),
                   "is" if len(unknown) == 1 else "are", "an option" if len(unknown) == 1 else "options")
    return kept


# ── A rule that names no environments, or no file, is refused ─────────────────

# Where a rule's environments come from. A push nothing names feeds no environment, so a rule has to say.
ENV_STRATEGIES = ("branch_map", "file_path_map", "branch_as_env", "pr_as_env", "tag_as_env")

# What a rule reads. A sync reads the file or files a rule names; nothing reads files no one named.
FILE_KEYS = ("file_name", "file_path_map")


def refusal(rule: dict) -> str:
    """Why *rule* (as config.cue and the tenant's bundle.json build it) is refused, or "". A rule must name where its
    environments come from, and the file it reads, in those layers: a repository's vartrack.json may change them but is
    not read here, and a rule that waited on it would be loaded by nobody who could say what it reads or writes."""
    no_env = not any(rule.get(key) for key in ENV_STRATEGIES)
    no_file = not any(rule.get(key) for key in FILE_KEYS)
    if not no_env and not no_file:
        return ""
    name = f"{rule.get('platform')}/{rule.get('datasource')}"
    env_fix = ("set branch_map (for example {\"main\": \"production\"}), file_path_map, branch_as_env, pr_as_env or "
               "tag_as_env in config.cue or the tenant's bundle.json. To keep writing to an environment called default, "
               "set branch_map: {\"main\": \"default\"}")
    file_fix = ("set file_name (for example \"app-settings.yaml\") or file_path_map in config.cue or the tenant's "
                "bundle.json. A repository's vartrack.json can still change it")
    if no_env and no_file:
        return f"{name} names no environments and no file to read: for the environments, {env_fix}; for the file, {file_fix}"
    if no_env:
        return f"{name} names no environments: {env_fix}"
    return f"{name} names no file to read: {file_fix}"


# What each tenant's refused rules are, as the last lookup found them: {tenant: {(platform, datasource): (rule, why)}}.
# A refused rule is not loaded, so nothing reaches it; the web UI lists it from here, with the reason.
_refused: dict[str, dict[tuple, tuple[dict, str]]] = {}
_refused_lock = threading.Lock()
_said: set[tuple] = set()


def refused_rules(tenant_id: str) -> list[tuple[dict, str]]:
    """(rule, why) of each of *tenant_id*'s rules that is refused."""
    with _refused_lock:
        return list(_refused.get(tenant_id, {}).values())


def refusal_of(tenant_id: str, platform: str, datasource: str) -> str:
    """Why the tenant's rule for *platform* and *datasource* is refused, or ""."""
    with _refused_lock:
        found = _refused.get(tenant_id, {}).get((platform, datasource))
    return found[1] if found else ""


def _say_refused(tenant_id: str, rule: dict, why: str) -> None:
    """Once per rule and reason: an error in the log, and a warning event on the tenant, so it is not silent."""
    key = (tenant_id, rule.get("platform"), rule.get("datasource"), why)
    if key in _said:
        return
    _said.add(key)
    logger.error("tenant %s: %s — the rule is refused and not loaded", tenant_id, why)
    try:
        from app.ui import ledger
        ledger.record_event(tenant=tenant_id, reason="RuleRefused", warning=True,
                            rule=f"{rule.get('platform')}/{rule.get('datasource')}", message=why)
    except Exception:
        logger.debug("RuleRefused event not recorded", exc_info=True)


def _keep(tenant_id: str, rules: list[dict]) -> list[dict]:
    """*rules* without the refused ones, which are remembered (and said) instead. Replaces what was remembered
    for the tenant: a rule that was fixed is no longer refused."""
    kept: list[dict] = []
    refused: dict[tuple, tuple[dict, str]] = {}
    for rule in rules:
        why = refusal(rule)
        if why:
            refused[(rule.get("platform"), rule.get("datasource"))] = (rule, why)
            _say_refused(tenant_id, rule, why)
        else:
            kept.append(rule)
    with _refused_lock:
        _refused[tenant_id] = refused
    return kept


def layered_rule(cue_rule: dict, bundle_rule: Optional[dict], tenant_id: str,
                 bundle: Optional[dict] = None, source: str = "bundle.json") -> dict:
    """config.cue's *cue_rule* with *bundle_rule* (bundle.json's, or None)
    laid over it, then its `force`."""
    bundle = _bundle() if bundle is None else bundle
    base = _base(cue_rule, bundle)
    child = (_settable_by_tenant(dict(with_current_names(bundle_rule, source)), base, source, tenant_id)
             if bundle_rule else {})
    rule = merge_child(base, child)
    rule["tenant_id"] = tenant_id   # the tenant it was loaded for, whatever a file said
    return apply_force(rule)


def _allowed(cue: dict, tenant_id: str) -> bool:
    """Whether *tenant_id*'s bundle.json may layer config.cue's rule *cue*."""
    tenants = cue.get("tenants") or []
    return not tenants or tenant_id in tenants


def owner(cue: dict) -> str:
    """The tenant a config.cue rule belongs to when no bundle.json names it: the first it
    lists in ``tenants``; else the one declared tenant whose repositories are the ones the
    rule covers; else "default"."""
    named = next(iter(cue.get("tenants") or []), None)
    if named:
        return named
    from app import tenants
    from app.pipeline.repo_scope import patterns
    include, _ = patterns(cue)
    covering = tenants.tenants_of_repositories(include)
    return covering[0] if len(covering) == 1 else DEFAULT_TENANT


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
        for r in bundle_rules:
            warn_tenant_claim(r, f"bundle.json: the {r.get('platform')}/{r.get('datasource')} rule",
                              f"the one whose schema repository holds this file ({tenant_id})", tenant_id)
        return _keep(tenant_id, [{**r, "tenant_id": tenant_id} for r in bundle_rules])
    out: list[dict] = []
    for rule in bundle_rules:
        cue = _find(cues, rule.get("platform"), rule.get("datasource"))
        if cue is not None and _usable(cue, rule, tenant_id):
            out.append(layered_rule(cue, rule, tenant_id, bundle))
    out.extend(layered_rule(cue, None, tenant_id, bundle)
               for cue in unclaimed(cues, claimed) if owner(cue) == tenant_id)
    return _keep(tenant_id, out)


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
        if bundle_rule is not None:
            warn_tenant_claim(bundle_rule, f"bundle.json: the {platform}/{datasource} rule",
                              f"the one whose schema repository holds this file ({tenant_id})", tenant_id)
        found = {**bundle_rule, "tenant_id": tenant_id} if bundle_rule is not None else None
        return _single(tenant_id, found)
    cue = _find(cues, platform, datasource)
    if bundle_rule is not None and not _usable(cue, bundle_rule, tenant_id):
        return None
    if cue is None:
        return None
    if bundle_rule is None and (tenant_id != owner(cue) or _claimed(cue, claimed)):
        return None
    return _single(tenant_id, layered_rule(cue, bundle_rule, tenant_id, bundle))


def _single(tenant_id: str, rule: Optional[dict]) -> Optional[dict]:
    """*rule*, or None when it is refused (remembered, and said, like the lists do)."""
    if rule is None:
        return None
    why = refusal(rule)
    key = (rule.get("platform"), rule.get("datasource"))
    with _refused_lock:
        if why:
            _refused.setdefault(tenant_id, {})[key] = (rule, why)
        else:
            _refused.get(tenant_id, {}).pop(key, None)
    if why:
        _say_refused(tenant_id, rule, why)
        return None
    return rule
