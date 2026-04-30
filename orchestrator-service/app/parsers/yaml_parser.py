"""
app/parsers/yaml_parser.py
───────────────────────────
Parse YAML / YML files.
"""
from __future__ import annotations

from typing import Any

try:
    import yaml
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def parse(content: str, file_path: str = "") -> Any:
    if not _AVAILABLE:
        raise RuntimeError(
            "PyYAML is not installed – cannot parse YAML. "
            "Run: pip install PyYAML"
        )
    return yaml.safe_load(content) or {}
