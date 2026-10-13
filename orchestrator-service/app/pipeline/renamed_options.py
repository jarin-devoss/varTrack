"""
Rule and datasource options that were renamed.

The old names are still read: a bundle.json, vartrack.json or config.cue
written with them keeps working, and a warning names the new spelling once
per source.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

RENAMED_OPTIONS: dict[str, str] = {
    "env_as_branch": "branch_as_env",
    "env_as_pr": "pr_as_env",
    "env_as_tags": "tag_as_env",
    "env_as_prefix": "prefix_as_env",  # s3
}

_warned: set[tuple[str, str]] = set()


def with_current_names(options: Any, source: str = "") -> Any:
    """
    *options* with renamed keys under their current names. When a key is set
    under both names, the current one wins. Anything but a dict is returned
    as is.
    """
    if not isinstance(options, dict) or not any(old in options for old in RENAMED_OPTIONS):
        return options
    out = dict(options)
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
