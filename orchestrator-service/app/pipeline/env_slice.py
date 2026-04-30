"""
app/pipeline/env_slice.py
──────────────────────────
Environment-aware data slicing, inserted between root_key extraction
and the flattener in stage_etl.py.

Three patterns are auto-detected — no rule config flag needed.

Pattern 1 — Top-level keys are environment names (all values are dicts)
────────────────────────────────────────────────────────────────────────
    {
        "predev":  { "age": 44, "color": "red", "name": "dan" },
        "default": { "age": 32, "color": "blue" }
    }
    env=predev  →  merge default first, then predev overrides
                →  { "age": 44, "color": "red", "name": "dan" }
    env=staging (unknown) → { "age": 32, "color": "blue" }  (default only)

Pattern 2 — Per-key env sub-keys mixed with scalars
────────────────────────────────────────────────────
    {
        "color": { "default": "blue", "predev": "green", "dev": "yellow" },
        "age": 43
    }
    env=predev  →  { "color": "green", "age": 43 }
    env=staging →  { "color": "blue",  "age": 43 }  (default fallback)

Pattern 3 — Hybrid: env-named dict keys mixed with scalar defaults
───────────────────────────────────────────────────────────────────
    {
        "prod":   { "name": "worn" },
        "predev": { "name": "dan" },
        "name":   "bob",
        "age":    33
    }
    env=prod    →  { "name": "worn", "age": 33 }  (prod overrides, scalars as defaults)
    env=predev  →  { "name": "dan",  "age": 33 }
    env=unknown →  { "name": "bob",  "age": 33 }  (scalars only — no matching env dict)

If the resolved env is a PR / branch / tag that doesn't appear in the data,
all patterns fall back to the "default" key silently.  If there is no
"default" key either, the data is returned unchanged.

This function is sink-agnostic.  The sink receives a clean flat-ready dict
plus the resolved env string it should use as its namespace / label.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def resolve_env_slice(data: dict, env: str) -> dict:
    """
    Slice env-keyed data for the given env.

    Parameters
    ----------
    data : dict
        Parsed dict after root_key extraction.
    env : str
        Resolved environment (branch name, "pr-42", "v1.2.3", mapped name …).

    Returns
    -------
    dict — env-resolved, flat-ready.  Returns data unchanged if no pattern
    is detected.
    """
    if not isinstance(data, dict) or not env:
        return data

    # ── Pattern 1 ─────────────────────────────────────────────────────────────
    # All values are dicts AND at least one key is the env or "default".
    all_values_dicts = all(isinstance(v, dict) for v in data.values())
    if all_values_dicts and (env in data or "default" in data):
        base     = dict(data.get("default", {}))
        override = dict(data.get(env, {}))
        merged   = {**base, **override}
        logger.debug(
            "env_slice pattern=1 env=%s base_keys=%d override_keys=%d",
            env, len(base), len(override),
        )
        return merged

    # ── Pattern 2 ─────────────────────────────────────────────────────────────
    # Mixed: some values are dicts with env sub-keys, others are plain scalars.
    result       : dict = {}
    pattern_2_hit = False

    for key, value in data.items():
        if isinstance(value, dict) and (env in value or "default" in value):
            pattern_2_hit = True
            result[key]   = value.get(env, value.get("default"))
            logger.debug("env_slice pattern=2 key=%s env=%s value=%r", key, env, result[key])
        else:
            result[key] = value  # scalar or unrelated nested dict — pass through

    if pattern_2_hit:
        return result

    # ── Pattern 3 ─────────────────────────────────────────────────────────────
    # Mixed top-level: some keys are env names (dict values), others are scalars.
    # Scalars act as universal defaults; the env-named dict overrides them.
    scalar_defaults = {k: v for k, v in data.items() if not isinstance(v, dict)}
    env_dicts       = {k: v for k, v in data.items() if isinstance(v, dict)}

    if env_dicts and scalar_defaults and (env in env_dicts or "default" in env_dicts):
        base     = dict(env_dicts.get("default", {}))
        override = dict(env_dicts.get(env, {}))
        merged   = {**scalar_defaults, **base, **override}
        logger.debug(
            "env_slice pattern=3 env=%s scalar_keys=%d env_dict_keys=%d",
            env, len(scalar_defaults), len(override),
        )
        return merged

    # ── No pattern matched ────────────────────────────────────────────────────
    return data


def detect_pattern(data: dict, env: str) -> str:
    """Return "pattern_1" | "pattern_2" | "pattern_3" | "none"  (used in debug logging)."""
    if not isinstance(data, dict) or not env:
        return "none"
    all_values_dicts = all(isinstance(v, dict) for v in data.values())
    if all_values_dicts and (env in data or "default" in data):
        return "pattern_1"
    for value in data.values():
        if isinstance(value, dict) and (env in value or "default" in value):
            return "pattern_2"
    scalar_defaults = {k: v for k, v in data.items() if not isinstance(v, dict)}
    env_dicts       = {k: v for k, v in data.items() if isinstance(v, dict)}
    if env_dicts and scalar_defaults and (env in env_dicts or "default" in env_dicts):
        return "pattern_3"
    return "none"
