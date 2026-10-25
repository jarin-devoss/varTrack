"""
app/utils/secret_file.py
────────────────────────
A {file: …} credential: the whole file, or one key of it.

    token:    {file: "/run/secrets/token"}                       # all of it, trimmed
    password: {file: "/run/secrets/db.env", key: "PASSWORD"}     # one key

With a key the file is parsed by its extension: .json, .yaml/.yml, otherwise
KEY=value lines (.env, .properties). In JSON and YAML a dotted key
("db.password") reaches into nested objects. The gateway and the watcher read
files the same way (ReadSecretFile).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def read_secret_file(path: str, key: Optional[str] = None) -> str:
    """The credential in *path*: all of it trimmed, or the value of *key*.

    Raises OSError when the file cannot be read and ValueError when it does
    not parse or has no such key.
    """
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    if not key:
        return content.strip()

    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        found, value = _lookup(json.loads(content), key)
    elif suffix in (".yaml", ".yml"):
        import yaml

        found, value = _lookup(yaml.safe_load(content), key)
    else:
        found, value = _env_value(content, key)
    if not found:
        raise ValueError(f"secret file {path} has no key {key!r}")
    if isinstance(value, (dict, list)):
        raise ValueError(f"secret file {path}: key {key!r} holds a {type(value).__name__}, not a single value")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _lookup(doc: Any, key: str) -> tuple[bool, Any]:
    """The key as written first, then as a dotted path through nested objects."""
    if isinstance(doc, dict) and key in doc:
        return True, doc[key]
    current = doc
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _env_value(content: str, key: str) -> tuple[bool, Optional[str]]:
    """KEY=value lines: blank lines and # comments skipped, "export " allowed,
    matching outer quotes removed."""
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        name, sep, value = line.partition("=")
        if not sep or name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = _unescape(value)
        return True, value
    return False, None


_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}


def _unescape(value: str) -> str:
    """Backslash escapes in a double-quoted value: quote, backslash, n, t, r."""
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value) and value[i + 1] in _ESCAPES:
            out.append(_ESCAPES[value[i + 1]])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)
