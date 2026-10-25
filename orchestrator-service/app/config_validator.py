"""
app/config_validator.py
───────────────────────
Checks a vartrack.json or a tenant's bundle.json before it is pushed, with
the code a sync runs: the presets (app.pipeline.presets), the keys each layer
may set and the rules config.cue has (app.pipeline.rule_layers). So what
passes here is what a sync takes.

It checks the names a vartrack.json "extends" without fetching them: a caller
never makes the orchestrator clone a repository. A bundle.json's presets are
fetched from the tenant's schema repo, as a sync does.

  errors      what a sync rejects: the file, a preset, a rule
  warnings    what a sync ignores
  migrations  options under a name that changed: a sync still reads them
"""
from __future__ import annotations

import json
import posixpath
from typing import Any

from app.pipeline.renamed_options import REMOVED_OPTIONS, RENAMED_OPTIONS, with_current_names
from app.pipeline.rule_options import STRING_MAPS as _MAPS, TYPES as _TYPES  # from rule.proto

_TYPE_NAMES = {dict: "an object", bool: "true or false", str: "a string", list: "a list", int: "a number",
               float: "a number"}
# Keys any file may carry that no layer reads.
_NOTES = frozenset({"description", "$schema"})


class Result:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.migrations: list[str] = []


def validate(file_path: str, content: str, tenant_id: str = "default") -> Result:
    """Check *content*, a file named *file_path*: vartrack.json, or
    bundle.json / rules.json of *tenant_id*."""
    result = Result()
    name = posixpath.basename(file_path.replace("\\", "/"))
    if name not in ("vartrack.json", "bundle.json", "rules.json"):
        result.errors.append(f"{name}: not a varTrack settings file — vartrack.json, bundle.json or rules.json")
        return result
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        result.errors.append(f"{name} is not valid JSON: {exc}")
        return result
    if name == "vartrack.json":
        _repo_config(data, result)
    else:
        _bundle(data, name, tenant_id, result)
    return result


def _unfetched(*_: Any) -> str:
    """A preset named, not fetched: its name (and platform) is all that is checked."""
    return "{}"


def _repo_config(data: Any, result: Result) -> None:
    from app.pipeline.presets import Origin, PresetError, Resolver
    from app.pipeline.repo_overrides import _OVERRIDABLE_KEYS

    if not isinstance(data, dict):
        result.errors.append(f"vartrack.json must hold a JSON object, not {type(data).__name__}")
        return
    try:
        Resolver(Origin(""), _unfetched).resolve(data)
    except PresetError as exc:
        result.errors.append(f"vartrack.json: {exc}")
    data = _current(data, "vartrack.json", result)
    ignored = sorted(set(data) - _OVERRIDABLE_KEYS - _NOTES - {"extends"})
    if ignored:
        result.warnings.append(f"vartrack.json cannot set {', '.join(ignored)} — a sync ignores "
                               f"{'it' if len(ignored) == 1 else 'them'}")
    _types({k: v for k, v in data.items() if k in _OVERRIDABLE_KEYS}, "vartrack.json", result)
    _prune_last(data, "vartrack.json", result)


def _bundle(data: Any, name: str, tenant_id: str, result: Result) -> None:
    from app.pipeline import rule_layers
    from app.pipeline.presets import Origin, PresetError, resolve_bundle_rules
    from app.schema_registry.manager import get_schema_manager

    schema_repo = get_schema_manager()._tenant_configs.get(tenant_id) or {}
    if schema_repo.get("repo_url"):
        origin, fetch = Origin.of(schema_repo["repo_url"], schema_repo.get("token")), None
    else:
        origin, fetch = Origin(""), _unfetched
    try:
        rules = resolve_bundle_rules(data, origin, name, fetch)
    except PresetError as exc:
        result.errors.append(str(exc))
        return

    bundle = rule_layers._bundle()
    cues = rule_layers.cue_rules(bundle)
    _repeated_rules(cues, "config.cue", result)
    _repeated_rules(rules, name, result)
    for i, rule in enumerate(rules, 1):
        platform, datasource = rule.get("platform"), rule.get("datasource")
        if not platform or not datasource:
            result.errors.append(f"{name}: rule {i} needs a platform and a datasource")
            continue
        where = f"{name}: the {platform}/{datasource} rule"
        rule = _current(rule, where, result)
        _types(rule, where, result)
        _prune_last(rule, where, result)
        if not cues:
            continue  # without rules in config.cue, bundle.json's are used as they are
        cue = next((c for c in cues if c.get("platform") == platform and c.get("datasource") == datasource), None)
        if cue is None:
            result.errors.append(f"{where}: config.cue has no rule for {platform}/{datasource}")
            continue
        if cue.get("tenants") and tenant_id not in cue["tenants"]:
            result.errors.append(f"{where} belongs to {', '.join(cue['tenants'])} (its tenants in config.cue), "
                                 f"not {tenant_id}")
            continue
        base = rule_layers.layered_rule(cue, None, tenant_id, bundle)
        ignored = sorted(k for k in rule if k in rule_layers.admin_only_keys() and rule[k] != base.get(k))
        if ignored:
            result.warnings.append(f"{where} sets {', '.join(ignored)}, which only config.cue sets — "
                                   f"a sync ignores {'it' if len(ignored) == 1 else 'them'}")
        _prune_without_effect(rule_layers.layered_rule(cue, rule, tenant_id, bundle), datasource, where, result)


def _repeated_rules(rules: list[dict], name: str, result: Result) -> None:
    """A rule is identified by its platform and datasource: a second rule for
    the pair is never used, since every lookup takes the first."""
    first: dict[tuple, int] = {}
    for i, rule in enumerate(rules, 1):
        key = (rule.get("platform"), rule.get("datasource"))
        if not all(key):
            continue  # reported as a rule without a platform or datasource
        if key in first:
            kind = str(key[1]).split("-", 1)[0]
            result.errors.append(
                f"{name}: rule {i} repeats {key[0]}/{key[1]} (rule {first[key]}): a rule is identified by its "
                f"platform and datasource; to write a second destination in the same database, declare a tagged "
                f"datasource (for example {kind}-db) and give it its own rule")
        else:
            first[key] = i


def _prune_last(options: dict, where: str, result: Result) -> None:
    """prune.last was removed: prune runs once per environment, after all its
    files are written. A prune block that only had it is a disabled block."""
    block = options.get("prune")
    if "prune_last" in options or (isinstance(block, dict) and "last" in block):
        enabled = isinstance(block, dict) and ("enabled" in block or "dry_run" in block)
        result.warnings.append(
            f"{where}: prune.last was removed and has no effect — prune now runs once per environment, "
            f"after all its files are written"
            + ("; a prune block with only `last` is disabled: write `prune: true`"
               if isinstance(block, dict) and not enabled else ""))


def _prune_without_effect(rule: dict, datasource: str, where: str, result: Result) -> None:
    """prune on a sink that replaces a whole object on every write does nothing."""
    from app.pipeline.datasource_settings import with_datasource_settings
    from app.pipeline.sinks import sink_class
    from app.tasks.payload import extract_prune_config

    cls = sink_class(datasource)
    if cls is None or not extract_prune_config(rule).enabled:
        return
    if not cls.prunes_keys(with_datasource_settings(datasource, rule)):
        result.warnings.append(f"{where}: prune has no effect on {datasource} — it replaces the whole "
                               f"{cls.object_name} on every write, so no stale key is left")


def _current(options: dict, where: str, result: Result) -> dict:
    result.migrations.extend(f"{where}: {old} is now {new}" for old, new in RENAMED_OPTIONS.items() if old in options)
    result.warnings.extend(f"{where}: {key} was removed and has no effect; {instead}"
                           for key, instead in REMOVED_OPTIONS.items() if key in options)
    return with_current_names(options)


def _types(options: dict, where: str, result: Result) -> None:
    for key, types in _TYPES.items():
        # True is an int in Python, not a number in JSON.
        if key in options and (not isinstance(options[key], types)
                               or (isinstance(options[key], bool) and bool not in types)):
            want = " or ".join(dict.fromkeys(_TYPE_NAMES[t] for t in types))
            result.errors.append(f"{where}: {key} must be {want}")
            continue
        if key in _MAPS and isinstance(options.get(key), dict):
            bad = sorted(k for k, v in options[key].items() if not isinstance(v, str))
            if bad:
                result.errors.append(f"{where}: {key} values must be strings ({', '.join(bad)})")
