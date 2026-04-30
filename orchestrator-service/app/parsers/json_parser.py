"""
app/parsers/json_parser.py
───────────────────────────
Parse JSON and JSONC (JSON-with-comments) files.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _strip_comments(text: str) -> str:
    """Remove // line comments and /* … */ block comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def parse(content: str, file_path: str = "") -> Any:
    if Path(file_path).suffix.lower() in (".jsonc", ".json5"):
        content = _strip_comments(content)
    return json.loads(content)
