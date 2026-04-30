"""
app/parsers/toml_parser.py
───────────────────────────
Parse TOML files (Cargo.toml, pyproject.toml, config.toml …).

Uses stdlib tomllib (Python 3.11+) or the tomli backport.
"""
from __future__ import annotations

from typing import Any

try:
    import tomllib as _toml      # Python 3.11+
    _AVAILABLE = True
except ImportError:
    try:
        import tomli as _toml  # type: ignore[no-redef]
        _AVAILABLE = True
    except ImportError:
        _AVAILABLE = False


def parse(content: str, file_path: str = "") -> Any:
    if not _AVAILABLE:
        raise RuntimeError(
            "tomllib/tomli not available – cannot parse TOML. "
            "Run: pip install tomli"
        )
    try:
        return _toml.loads(content)
    except Exception as exc:
        raise ValueError(
            f"TOML parse error in {file_path!r}: {exc}"
        ) from exc
