"""
app/parsers/kv_parser.py
─────────────────────────
Parse KEY=VALUE style files:
  - .env  /  .env.staging  /  .env.production
  - .properties (Java)
  - .npmrc  /  .yarnrc
  - simple Makefile variable assignments
  - Dockerfile ENV instructions (ENV KEY=VALUE  or  ENV KEY VALUE)

Rules
─────
- Lines starting with # ; ! are comments → skipped
- Blank lines → skipped
- KEY=VALUE  or  KEY: VALUE  (first separator wins)
- Surrounding quotes on values are stripped (single or double)
- For Dockerfile ENV VAR VALUE (space-separated, no =), we handle
  it as a special case when the key has no separator but the line
  has exactly two whitespace-separated tokens.
"""
from __future__ import annotations


def parse(content: str, file_path: str = "") -> dict[str, str]:
    result: dict[str, str] = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()

        # Skip blank lines and comments
        if not line or line[0] in ("#", ";", "!"):
            continue

        # Dockerfile: ENV KEY VALUE (no equals sign, two tokens)
        if line.upper().startswith("ENV "):
            rest = line[4:].strip()
            if "=" not in rest:
                parts = rest.split(None, 1)
                if len(parts) == 2:
                    result[parts[0]] = parts[1]
                continue
            line = rest   # fall through to normal KEY=VALUE parsing

        # export KEY=VALUE (shell-style)
        if line.startswith("export "):
            line = line[7:].strip()

        # KEY=VALUE or KEY: VALUE
        for sep in ("=", ":"):
            if sep in line:
                k, _, v = line.partition(sep)
                k = k.strip()
                v = v.strip()
                # Strip surrounding quotes
                if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
                    v = v[1:-1]
                if k:
                    result[k] = v
                break

    return result
