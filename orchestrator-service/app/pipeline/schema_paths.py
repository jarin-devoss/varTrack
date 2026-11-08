"""
app/pipeline/schema_paths.py
────────────────────────────
The paths of schema_map: the glob that says which files an entry covers, the checks on an entry, and the safe
join of a schema path to the tenant's schema repository.

The checks are the ones libs/go/bundle/schema_map.go makes when config.cue loads, so an entry is judged the same
way wherever it is written (config.cue, or a tenant's bundle.json).

A glob over a file's path in its repository, with forward slashes:

    *      anything but "/"            services/*/app.yaml
    **     anything, "/" included      services/**/app.yaml  (also services/app.yaml)
    ?      one character but "/"
    [abc]  one character of the set    [!abc] / [^abc] one character not in it

Dotfiles match, and case counts. ``pathlib.PurePath.full_match`` does ``**`` but only from Python 3.13, so the
matcher is this module's own.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

NONE = "none"          # a schema_map schema that means "not checked, on purpose"


def glob_problem(pattern: str) -> str:
    """Why *pattern* is not a usable schema_map match, or "" when it is."""
    if pattern == "":
        return "is empty"
    if "\\" in pattern:
        return 'has a backslash: paths use "/"'
    if pattern.startswith("/"):
        return "starts with /: a path in the repository is relative to its root"
    if "***" in pattern:
        return 'has "***"'
    i = 0
    while i < len(pattern):
        if pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end < 0:
                return 'has a "[" with no closing "]"'
            if end == i + 1:
                return 'has an empty "[]" class'
            i = end
        i += 1
    return ""


def schema_problem(schema: str) -> str:
    """Why *schema* is not a usable schema_map schema, or "" when it is: "none", or a relative path of
    "/"-separated names ending in .cue, with no "..", inside the schema repository."""
    if schema == NONE:
        return ""
    if schema == "":
        return 'is empty (a path to a .cue file, or "none")'
    if "\\" in schema:
        return 'has a backslash: paths use "/"'
    if schema.startswith("/") or (len(schema) > 1 and schema[1] == ":"):
        return "is an absolute path: it is relative to the schema repository"
    if not schema.endswith(".cue"):
        return 'does not end in ".cue"'
    if "\0" in schema:
        return "has a NUL byte"
    for part in schema.split("/"):
        if part == "..":
            return 'has a ".." segment: it cannot leave the schema repository'
        if part == "":
            return 'has an empty segment ("//")'
    return ""


def schema_map_problems(entries: Any, where: str = "schema_map") -> list[str]:
    """Every problem of a rule's schema_map, each naming its entry: [] when it is usable. *entries* is what the
    JSON holds, so a wrong shape is a problem too, not a crash."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        return [f"{where} must be a list of {{match, schema}}, not {type(entries).__name__}"]
    problems: list[str] = []
    for i, entry in enumerate(entries):
        label = f"{where}[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{label} must be {{match, schema}}, not {type(entry).__name__}")
            continue
        unknown = sorted(set(entry) - {"match", "schema"})
        if unknown:
            problems.append(f"{label} has {', '.join(unknown)}: an entry is {{match, schema}}")
        for key, check in (("match", glob_problem), ("schema", schema_problem)):
            value = entry.get(key, "")
            if not isinstance(value, str):
                problems.append(f"{label}.{key} must be a string")
            elif problem := check(value):
                problems.append(f'{label}.{key} "{value}": {problem}')
    return problems


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> "re.Pattern[str]":
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                i += 2
                if pattern.startswith("/", i):       # "**/": zero or more whole folders
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end < 0:                               # unclosed: a literal "[" (the load check refuses it)
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:end]
                negate = body[:1] in ("!", "^")
                body = body[1:] if negate else body
                body = body.replace("\\", "\\\\")
                out.append(f"[{'^' if negate else ''}{body}]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def glob_match(pattern: str, path: str) -> bool:
    """Whether the file at *path* (forward slashes, relative to its repository) is covered by *pattern*."""
    return _compile(pattern).match(path.replace("\\", "/").lstrip("/")) is not None


def join_inside(root: Path, relative: str) -> Path:
    """*relative* joined to *root*, which has to stay inside it once symlinks are resolved.

    Raises ValueError for an absolute path, a ".." that leaves, or a symlink in the clone that points out of it."""
    if schema_problem(relative) not in ("",):
        raise ValueError(f"schema path {relative!r} {schema_problem(relative)}")
    base = root.resolve()
    target = (base / relative).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"schema path {relative!r} leaves the schema repository")
    return target
