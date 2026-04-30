"""
app/parsers/loader.py
──────────────────────
Single public interface for parsing *any* settings file.

Usage
─────
    from app.parsers.loader import load

    data = load(content, file_path="appsettings.Production.json")
    # → nested dict/list, ready for the flattener

How it works
────────────
1. detector.detect(file_path)  →  format string
2. Route to the matching parser module
3. If format == "sniff": try every parser in order until one succeeds
4. Return the parsed object (dict or list)

The calling code (ETLTransformer) never needs to know which format
the file is — it just calls load() and gets back a Python structure.
"""
from __future__ import annotations

import logging
from typing import Any

from app.parsers import detector

logger = logging.getLogger(__name__)

# ── Sniffer order when format cannot be determined from the filename ───────────
# Each entry is (format_name, callable)
_SNIFF_ORDER = [
    ("json",  lambda c, fp: __import__("app.parsers.json_parser", fromlist=["parse"]).parse(c, fp)),
    ("yaml",  lambda c, fp: __import__("app.parsers.yaml_parser", fromlist=["parse"]).parse(c, fp)),
    ("toml",  lambda c, fp: __import__("app.parsers.toml_parser", fromlist=["parse"]).parse(c, fp)),
    ("hcl",   lambda c, fp: __import__("app.parsers.hcl_parser",  fromlist=["parse"]).parse(c, fp)),
    ("xml",   lambda c, fp: __import__("app.parsers.xml_parser",  fromlist=["parse"]).parse(c, fp)),
    ("ini",   lambda c, fp: __import__("app.parsers.ini_parser",  fromlist=["parse"]).parse(c, fp)),
    ("kv",    lambda c, fp: __import__("app.parsers.kv_parser",   fromlist=["parse"]).parse(c, fp)),
]


def load(content: str, file_path: str) -> Any:
    """
    Parse *content* of the file at *file_path* and return a Python object.

    Parameters
    ----------
    content:
        Raw UTF-8 string content of the file (already read from git).
    file_path:
        The relative path / filename as stored in the repo
        (e.g. "config/appsettings.Production.json", ".env.staging").
        Used only for format detection — the file doesn't need to exist
        on disk.

    Returns
    -------
    dict | list
        Parsed representation, ready to be flattened.

    Raises
    ------
    ValueError
        If content could not be parsed by any known format.
    """
    fmt = detector.detect(file_path)
    logger.debug("load file=%s detected_format=%s", file_path, fmt)

    if fmt != "sniff":
        return _parse_with(fmt, content, file_path)

    # Unknown extension — try every parser in sequence
    return _sniff(content, file_path)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_with(fmt: str, content: str, file_path: str) -> Any:
    """Dispatch to the correct parser module by format string."""
    from app.parsers import (
        json_parser, yaml_parser, toml_parser,
        xml_parser, ini_parser, kv_parser, hcl_parser,
    )
    parsers = {
        "json": json_parser.parse,
        "yaml": yaml_parser.parse,
        "toml": toml_parser.parse,
        "xml":  xml_parser.parse,
        "ini":  ini_parser.parse,
        "kv":   kv_parser.parse,
        "hcl":  hcl_parser.parse,
    }
    fn = parsers.get(fmt)
    if fn is None:
        raise ValueError(f"Unknown format: {fmt!r}")
    return fn(content, file_path)


def _sniff(content: str, file_path: str) -> Any:
    """
    Try every parser in order and return the first non-empty result.
    Last resort: return whatever kv_parser produces (always succeeds).
    """
    stripped = content.strip()

    # Cheap heuristic short-circuits before trying all parsers
    if stripped.startswith(("{", "[")):
        _formats = ["json", "yaml", "toml", "ini", "kv"]
    elif stripped.startswith("<"):
        _formats = ["xml", "kv"]
    elif "[" in stripped:
        _formats = ["toml", "ini", "yaml", "json", "hcl", "kv"]
    else:
        _formats = ["yaml", "json", "toml", "hcl", "ini", "kv"]

    for fmt in _formats:
        try:
            result = _parse_with(fmt, content, file_path)
            if result:
                logger.debug("sniff succeeded with format=%s for %s", fmt, file_path)
                return result
        except Exception:
            continue

    # kv_parser always returns something (even if empty)
    from app.parsers import kv_parser
    return kv_parser.parse(content, file_path)
