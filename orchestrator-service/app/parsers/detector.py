"""
app/parsers/detector.py
────────────────────────
Maps any file path / name → a format string.

The file tracked by a rule can be literally anything:
    package.json, appsettings.Production.json, .env.staging,
    config.toml, secrets.xml, uwsgi.cfg, Makefile, variables.tfvars …

Detection order (first match wins)
───────────────────────────────────
1. Exact basename match      (.env, Makefile, web.config …)
2. Basename-prefix match     (.env.staging starts with ".env")
3. Innermost file extension  (appsettings.Production.json → .json)
4. "sniff"                   caller will try every parser in sequence

Returned format strings
───────────────────────
  "json"  "yaml"  "toml"  "xml"  "ini"  "kv"  "hcl"  "sniff"
"""
from __future__ import annotations

from pathlib import Path

# ── Basenames that always imply a format ──────────────────────────────────────
# Keys are lowercased basenames.
_BASENAME: dict[str, str] = {
    # dotenv family
    ".env":        "kv",
    ".envrc":      "kv",
    # npm / yarn
    ".npmrc":      "kv",
    ".yarnrc":     "kv",
    # misc posix
    ".netrc":      "kv",
    "procfile":    "kv",
    "makefile":    "kv",   # we only capture export VAR=VALUE lines
    "dockerfile":  "kv",   # ENV VAR=VALUE lines
    # .NET XML configs
    "app.config":       "xml",
    "web.config":       "xml",
    "nlog.config":      "xml",
    "log4net.config":   "xml",
    "appsettings.json": "json",   # catch-all for the bare name
    "application":      "yaml",   # Spring Boot (application, application-prod)
    "settings":         "yaml",   # Django / generic Python apps
    "values":           "yaml",   # Helm values (without .yaml suffix)
    "variables":        "yaml",   # Ansible / Terraform variable files
    "secrets":          "yaml",   # generic secrets manifest
    "database":         "yaml",   # generic database config
    # HCL/Terraform without extension
    "main":             "hcl",    # main.tf without extension
    "outputs":          "hcl",    # outputs.tf without extension
    "providers":        "hcl",    # providers.tf without extension
}

# ── Extension → format ────────────────────────────────────────────────────────
_EXT: dict[str, str] = {
    ".json":       "json",
    ".jsonc":      "json",   # JSON-with-comments, stripped before parse
    ".json5":      "json",
    ".yaml":       "yaml",
    ".yml":        "yaml",
    ".toml":       "toml",
    ".xml":        "xml",
    ".ini":        "ini",
    ".cfg":        "ini",
    ".conf":       "ini",
    ".config":     "ini",
    ".properties": "kv",
    ".env":        "kv",
    ".tfvars":     "kv",
    ".tf":         "hcl",
    ".hcl":        "hcl",
}


def detect(file_path: str) -> str:
    """
    Return a format string for *file_path*.

    Examples
    --------
    detect("package.json")                  → "json"
    detect("appsettings.Production.json")   → "json"
    detect(".env.staging")                  → "kv"
    detect("config.toml")                   → "toml"
    detect("web.config")                    → "xml"
    detect("uwsgi.cfg")                     → "ini"
    detect("variables.tfvars")              → "kv"
    detect("some_unknown_file")             → "sniff"
    """
    p = Path(file_path)
    basename = p.name.lower()

    # 1. Exact basename
    if basename in _BASENAME:
        return _BASENAME[basename]

    # 2. Basename-prefix  (.env.staging, .env.prod, .env.local …)
    for prefix, fmt in _BASENAME.items():
        if basename.startswith(prefix + "."):
            return fmt

    # 3. Innermost extension (handles compound names)
    for ext in reversed([s.lower() for s in p.suffixes]):
        if ext in _EXT:
            return _EXT[ext]

    return "sniff"
