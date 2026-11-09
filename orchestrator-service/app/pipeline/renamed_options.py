"""
Rule and datasource options that were renamed or removed.

The old names are still read: a bundle.json, vartrack.json or config.cue
written with them keeps working, and a warning names the new spelling once
per source. Which names changed is stated on the option itself, in the proto
(vartrack.v1.config old_names), and read from there (rule_options.RENAMED).

A removed option is dropped, with a warning that says what to do instead: a
config that still sets it is not rejected, but it does not take effect, and
the warning says so.
"""
from __future__ import annotations

import logging
from typing import Any

from app.pipeline.rule_options import RENAMED as RENAMED_OPTIONS  # old name → current, from the protos

logger = logging.getLogger(__name__)

# Rule options that were removed, and what to do instead.
REMOVED_OPTIONS: dict[str, str] = {
    "secrets": "declare the secret manager under secret_managers in config.cue (path_prefix is set there) "
               "and name it in the rule's secret_manager",
    "retry_backoff": "nothing read it",
    "dry_run": "a dry run is started, not configured: use the UI's Dry run, `vt sync --dry-run` or the dry-run webhook; "
               "the rule now writes on every push",
    "dry_run_prune": "a dry run reports what a prune would delete; a rule with prune off deletes nothing",
    "max_changed_files": "a rule reads only the files it names (file_name or file_path_map): a push that touched other "
                         "files does not sync them, so there is nothing to cut",
}

# Options of the prune block that were removed.
REMOVED_PRUNE_OPTIONS: dict[str, str] = {
    "dry_run": "a dry run reports what a prune would delete; a rule with prune off deletes nothing",
}

_warned: set[tuple[str, str]] = set()


def with_current_names(options: Any, source: str = "") -> Any:
    """
    *options* with renamed keys under their current names. When a key is set
    under both names, the current one wins. Anything but a dict is returned
    as is.
    """
    prune = options.get("prune") if isinstance(options, dict) else None
    if not isinstance(options, dict) or not (
        any(key in options for key in (*RENAMED_OPTIONS, *REMOVED_OPTIONS))
        or (isinstance(prune, dict) and any(key in prune for key in REMOVED_PRUNE_OPTIONS))
    ):
        return options
    out = dict(options)
    if isinstance(prune, dict) and any(key in prune for key in REMOVED_PRUNE_OPTIONS):
        out["prune"] = {k: v for k, v in prune.items() if k not in REMOVED_PRUNE_OPTIONS}
        for key, instead in REMOVED_PRUNE_OPTIONS.items():
            if key in prune and (source, f"prune.{key}") not in _warned:
                _warned.add((source, f"prune.{key}"))
                logger.warning("%s: prune.%s was removed and has no effect; %s", source or "rule", key, instead)
    for key, instead in REMOVED_OPTIONS.items():
        if key in out:
            del out[key]
            if (source, key) not in _warned:
                _warned.add((source, key))
                logger.warning("%s: %s was removed and has no effect; %s", source or "rule", key, instead)
    for old, current in RENAMED_OPTIONS.items():
        if old not in out:
            continue
        value = out.pop(old)
        out.setdefault(current, value)
        if (source, old) not in _warned:
            _warned.add((source, old))
            logger.warning("%s: %s is now %s; the old name still works", source or "rule", old, current)
    return out


def upgrade_bundle(bundle: Any, source: str = "") -> Any:
    """A bundle (cue export) with its rules' and S3 datasources' renamed options under their current names."""
    if not isinstance(bundle, dict):
        return bundle
    if isinstance(bundle.get("rules"), list):
        bundle["rules"] = [with_current_names(r, source) for r in bundle["rules"]]
    for wrapper in bundle.get("datasources") or []:
        if isinstance(wrapper, dict) and isinstance(wrapper.get("s3"), dict):
            wrapper["s3"] = with_current_names(wrapper["s3"], source)
    return bundle
