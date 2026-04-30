"""
app/pipeline/transformer.py
────────────────────────────
Stage 2 transformer: parse content → flatten → apply variables_map overlay.

Design
──────
transform() is a pure function — it takes raw file content and rule
parameters, and returns a flat dict.  No side effects, no I/O.

Flattening strategy
───────────────────
All config formats are normalised to a flat dict with dot-separated keys:

  { "database.host": "localhost", "database.port": "5432" }

The Rust core (flatten.rs) is used when available for performance; otherwise
a pure-Python fallback handles the flattening.  The Python API never changes,
only the execution engine underneath.

Variables map overlay
─────────────────────
variables_map is an optional dict that remaps keys after flattening:

  variables_map: { "DB_HOST": "database.host" }

After the overlay: { "DB_HOST": "localhost" }

Keys NOT in variables_map keep their original flat names.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def transform(
    *,
    content: str,
    file_path: str,
    variables_map: dict[str, str],
    env: str,
    branch: str | None = None,
    repo: str | None = None,
    pr_number: str | None = None,
    tag: str | None = None,
    root_key: str = "",
) -> dict[str, str]:
    """
    Parse *content*, flatten it to dot-separated keys, apply *variables_map*.

    Parameters
    ----------
    content       : raw file content (JSON, YAML, TOML, .env, HCL, …)
    file_path     : original file path — used to pick the right parser
    variables_map : optional key remapping; empty dict = no remapping
    env           : resolved environment string (for metadata injection)
    branch        : git branch (for metadata)
    repo          : repository URL (for metadata)
    pr_number     : PR number if applicable (for metadata)
    tag           : git tag if applicable (for metadata)
    root_key      : if non-empty, extract this sub-tree before flattening
                    (already extracted by stage_etl when passed here as "")

    Returns
    -------
    dict[str, str] — flat key→value pairs, all values coerced to str.
                     Returns empty dict on parse / flatten error.
    """
    # ── 1. Parse raw content ──────────────────────────────────────────────────
    try:
        from app.parsers.loader import load as parse_file
        parsed = parse_file(content, file_path)
    except Exception:
        logger.exception("transformer: parse failed file=%s", file_path)
        return {}

    if parsed is None:
        logger.debug("transformer: empty parse result file=%s", file_path)
        return {}

    # ── 2. Optional root_key extraction ──────────────────────────────────────
    if root_key and isinstance(parsed, dict) and root_key in parsed:
        parsed = parsed[root_key]

    # ── 3. Flatten ────────────────────────────────────────────────────────────
    flat = _flatten(parsed)

    if not flat:
        logger.debug("transformer: empty flat result file=%s", file_path)
        return {}

    # ── 4. Apply variables_map overlay ────────────────────────────────────────
    # variables_map: { "NEW_KEY": "original.flat.key" }
    # Keys NOT mentioned in variables_map are passed through unchanged.
    if variables_map:
        for new_key, src_key in variables_map.items():
            if src_key in flat:
                flat[new_key] = flat.pop(src_key)
            else:
                logger.debug(
                    "transformer: variables_map src_key=%r not in flat data file=%s",
                    src_key, file_path,
                )

    logger.debug(
        "transformer: file=%s env=%s keys=%d", file_path, env, len(flat)
    )
    return flat


# ── Flatten implementation ────────────────────────────────────────────────────

def _flatten(data: Any, prefix: str = "") -> dict[str, str]:
    """
    Recursively flatten a nested dict/list structure to dot-separated keys.

    Tries the Rust core first (compiled via Maturin/PyO3).  Falls back to
    the pure-Python implementation if the Rust extension is not available.

    Returns dict[str, str] — all values coerced to str.
    """
    try:
        from vartrack_core import py_flatten_bfs as rust_flatten  # type: ignore[import]
        # Rust flatten expects a JSON string, returns a JSON string of {str: str}
        payload = json.dumps(data)
        result_json = rust_flatten(payload)
        return json.loads(result_json)
    except ImportError:
        pass   # Rust extension not compiled — use Python fallback
    except Exception:
        logger.debug("transformer: rust flatten failed, using Python fallback")

    return _py_flatten(data, prefix)


def _py_flatten(data: Any, prefix: str = "") -> dict[str, str]:
    """
    Iterative flatten using an explicit stack — avoids Python call-stack
    overflow on deeply nested configs.

    Handles dict, list, tuple, set, frozenset — all sequence types, not just list.

    Returns dict[str, str] — all values coerced to str.
    """
    result: dict[str, str] = {}
    stack: list[tuple[Any, str]] = [(data, prefix)]

    while stack:
        node, pfx = stack.pop()

        if isinstance(node, dict):
            for key, value in node.items():
                new_pfx = f"{pfx}.{key}" if pfx else str(key)
                stack.append((value, new_pfx))

        elif isinstance(node, (list, tuple, set, frozenset)):
            # Handle ALL sequence types, not just list.  Sets/frozensets get sorted for determinism.
            if isinstance(node, (set, frozenset)):
                items = enumerate(sorted(str(v) for v in node))
            else:
                items = enumerate(node)
            for i, item in items:
                new_pfx = f"{pfx}.{i}" if pfx else str(i)
                stack.append((item, new_pfx))

        else:
            # Leaf node — coerce to str
            if node is None:
                result[pfx] = ""
            elif isinstance(node, bool):
                result[pfx] = "true" if node else "false"
            elif isinstance(node, float):
                result[pfx] = str(node)
            else:
                result[pfx] = str(node)

    return result
