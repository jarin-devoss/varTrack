"""
app/parsers/ini_parser.py
──────────────────────────
Parse INI / CFG / CONF files using stdlib configparser.

Produces a nested dict:
    [database]
    host = localhost
    port = 5432

→ {"database": {"host": "localhost", "port": "5432"}}

Top-level keys without a section header land at the root level.
"""
from __future__ import annotations

import configparser
from typing import Any


def parse(content: str, file_path: str = "") -> Any:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str  # type: ignore[assignment]  # preserve case
    cfg.read_string(content)

    result: dict[str, Any] = {}

    # Keys in [DEFAULT] / before any section
    for k, v in cfg.defaults().items():
        result[k] = v

    for section in cfg.sections():
        result[section] = dict(cfg[section])

    return result
