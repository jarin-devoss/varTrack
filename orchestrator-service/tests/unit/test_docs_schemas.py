"""
tests/unit/test_docs_schemas.py

The schema examples in the docs are run.

A schema in README.md or docs/ is fenced ```cue schema. The blocks after it, up to the next heading, that are fenced
```yaml data, ```json data, ```toml data, ```ini data or ```env data are files it is run on: each must pass the
schema, or, fenced `data fails`, must be refused. A block that fails starts with one comment line, `# expect: <text>`
(`// expect: <text>` in JSON), and the error CUE gives must contain that text. Every schema needs at least one file.

`libs/go/bundle/docs_test.go` skips ```cue schema blocks: they are schemas, not config.cue.

Files under examples/ are run the same way: each schema with the config file that passes it, and with the same file
made wrong.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.parsers.loader import load
from app.pipeline import transformer
from app.pipeline.validator import CUEValidationError, Validator

ROOT = Path(__file__).resolve().parents[3]
FENCE = re.compile(r"^(\s*)```(\w+)(?:\s+(.*))?$")
CLOSE = re.compile(r"^\s*```\s*$")
HEADING = re.compile(r"^#{1,6}\s")
EXTENSIONS = {"yaml": "yaml", "yml": "yml", "json": "json", "toml": "toml", "ini": "ini", "env": "env"}
EXPECT = re.compile(r"^\s*(?:#|//)\s*expect:\s*(.+?)\s*$")

needs_cue = pytest.mark.skipif(shutil.which("cue") is None and not os.environ.get("CI"),
                               reason="the cue program is not installed")


def check(schema_text: str, name: str, text: str, tmp: Path):
    """Parse, flatten and validate as a sync does. Returns None when the file passes, else CUE's error."""
    schema = tmp / "schema.cue"
    schema.write_text(schema_text, encoding="utf-8")
    parsed = load(text, name)
    structured = isinstance(parsed, (dict, list))
    flat = transformer.transform(content=text, file_path=name, variables_map={}, env="", root_key="",
                                 parsed=parsed if structured else None)
    try:
        Validator(schema_dir=tmp).validate(flat, name, strict=True, schema_path=schema,
                                           own=transformer.own_forms(parsed if structured else None, flat))
    except CUEValidationError as exc:
        return str(exc)
    return None


# ── the docs ──────────────────────────────────────────────────────────────────

def doc_cases():
    """(where, schema text, data file name, data text, fails, expected text) for every pair in the docs."""
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    cases, schemas = [], []
    for md in files:
        lines = md.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        schema = None
        i = 0
        while i < len(lines):
            if HEADING.match(lines[i]):
                schema = None
            m = FENCE.match(lines[i])
            if not m:
                i += 1
                continue
            indent, lang, info = m.group(1), m.group(2), (m.group(3) or "").split()
            start, body = i + 1, []
            i += 1
            while i < len(lines) and not CLOSE.match(lines[i]):
                body.append(lines[i][len(indent):] if lines[i].startswith(indent) else lines[i].lstrip())
                i += 1
            i += 1
            where = f"{md.relative_to(ROOT).as_posix()}:{start}"
            if lang == "cue" and "schema" in info:
                schema = (where, "\n".join(body) + "\n")
                schemas.append(where)
            elif schema is not None and lang in EXTENSIONS and "data" in info:
                fails = "fails" in info
                expected = None
                if body and EXPECT.match(body[0]):
                    expected = EXPECT.match(body[0]).group(1)
                    body = body[1:]
                cases.append((f"{where} (schema {schema[0]})", schema[0], schema[1],
                              f"app.{EXTENSIONS[lang]}", "\n".join(body) + "\n", fails, expected))
    return cases, schemas


CASES, SCHEMAS = doc_cases()


def test_the_docs_have_schema_examples_and_each_has_a_file():
    assert SCHEMAS, "no ```cue schema block in README.md or docs/"
    with_data = {case[1] for case in CASES}
    assert [s for s in SCHEMAS if s not in with_data] == [], "a ```cue schema block with no data block after it"


def test_every_refused_file_says_what_to_expect():
    assert [c[0] for c in CASES if c[5] and not c[6]] == [], "a `data fails` block with no `expect:` first line"


@needs_cue
@pytest.mark.parametrize("where, schema_where, schema, name, text, fails, expected", CASES, ids=[c[0] for c in CASES])
def test_a_doc_example_does_what_the_page_says(tmp_path, where, schema_where, schema, name, text, fails, expected):
    error = check(schema, name, text, tmp_path)
    if fails:
        assert error is not None, f"{where}: the page says this file is refused, and it passes"
        assert expected in error, f"{where}: expected {expected!r} in the error:\n{error}"
    else:
        assert error is None, f"{where}: the page says this file passes:\n{error}"


# ── the examples directory ────────────────────────────────────────────────────

def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


@needs_cue
def test_annotated_schema_passes_the_config_that_is_written_for_it(tmp_path):
    schema, config = read("examples/schemas/annotated_schema.cue"), read("examples/configs/annotated.yaml")
    assert check(schema, "annotated.yaml", config, tmp_path) is None


@needs_cue
@pytest.mark.parametrize("change, key", [
    ("app_port: 8080", "app_port"), ("log_level: info", "log_level"), ("dark_mode: false", "feature.dark_mode"),
    ("max_connections: 200", "database.max_connections"), ("ttl_seconds: 300", "cache.ttl_seconds"),
])
def test_annotated_schema_refuses_each_wrong_value(tmp_path, change, key):
    bad = {"app_port: 8080": "app_port: 80", "log_level: info": "log_level: loud",
           "dark_mode: false": "dark_mode: maybe", "max_connections: 200": "max_connections: 5000",
           "ttl_seconds: 300": "ttl_seconds: -1"}[change]
    config = read("examples/configs/annotated.yaml")
    assert change in config
    error = check(read("examples/schemas/annotated_schema.cue"), "annotated.yaml", config.replace(change, bad), tmp_path)
    assert error is not None and key in error


@needs_cue
def test_the_env_example_passes_its_schema_and_a_number_that_is_not_one_does_not(tmp_path):
    schema, env = read("examples/schemas/vars.env.cue"), read("examples/configs/vars.env")
    assert check(schema, "vars.env", env, tmp_path) is None
    error = check(schema, "vars.env", env.replace("SERVER_PORT=8080", "SERVER_PORT=80a"), tmp_path)
    assert error is not None and 'SERVER_PORT = "80a" in vars.env' in error
    error = check(schema, "vars.env", env.replace("DB_SSL=true", "DB_SSL=yes"), tmp_path)
    assert error is not None and "DB_SSL" in error
    error = check(schema, "vars.env", env.replace("SERVER_PORT=8080", "SERVER_PORT=80"), tmp_path)
    assert error is not None and "SERVER_PORT" in error                 # below the range
