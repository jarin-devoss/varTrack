"""
app/utils/cue/__init__.py

Utilities for working with CUE schema files.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def find_cue_files(schema_root: Path, glob: str = "**/*.cue") -> list[Path]:
    """Return all .cue files under schema_root, sorted for determinism."""
    if not schema_root.exists():
        return []
    return sorted(schema_root.glob(glob))


def unflatten(flat: dict[str, str], sep: str = ".") -> dict:
    """Convert flat dot-notation dict → nested structure."""
    result: dict = {}
    for key, value in flat.items():
        parts = key.split(sep)
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def flat_to_json(flat: dict[str, str], indent: int = 2) -> str:
    """Convert a flat dict to nested JSON string."""
    return json.dumps(unflatten(flat), indent=indent)


def tenant_schema_path(registry_root: Path, tenant_id: str) -> Path:
    """
    Each tenant's schemas live in <registry_root>/<tenant_id>/.
    Falls back to registry root if no tenant subdirectory exists.
    """
    tenant_dir = registry_root / tenant_id
    if tenant_dir.is_dir():
        return tenant_dir
    return registry_root
