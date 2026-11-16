"""
tests/unit/test_readings.py

What `cue vet` is shown (app/pipeline/readings.py), and what a schema can then say.

The rule table and the typed view are plain unit tests. The schema table runs the real `cue` program and is skipped
where it is not installed.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.parsers.loader import load
from app.pipeline import readings, transformer
from app.pipeline.validator import CUEValidationError, Validator


# ── the reading rule ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("8080", "8080"), ("0", "0"), ("-5", "-5"), ("1000000000000000000000", "1000000000000000000000"),
    ("1.10", "1.10"), ("-0.5", "-0.5"), ("2.5e10", "2.5e10"), ("1e5", "1e5"), ("1E5", "1E5"), ("1e-05", "1e-05"),
    ("1e+16", "1e+16"), ("-0", "-0"),
    ("true", "true"), ("TRUE", "true"), ("False", "false"),
    # never a number: JSON's grammar refuses them, though CUE would read many of them
    ("0123", None), ("+5", None), ("1_000", None), ("0x1F", None), ("0o17", None), ("0b101", None),
    (" 5", None), ("5 ", None), ("\t5", None), ("5\n", None), ("", None),
    (".5", None), ("5.", None), ("NaN", None), ("nan", None), ("Infinity", None), ("-Infinity", None), ("inf", None),
    ("1e999", None), ("-", None), ("--5", None), ("5-", None),
    ("null", None), ('"5"', None), ("[1]", None), ("{}", None), ("[" * 5000, None), ("١٢٣", None),
    ("yes", None), ("no", None), ("on", None), ("off", None), ("1.2.3", None), ("checkout", None),
    ("9" * 5000, None),                                  # more digits than Python reads: text, not an error
])
def test_text_reads_as(text, expected):
    assert readings.other_reading(text) == expected


@pytest.mark.parametrize("text, own, expected", [
    # the file's own form is the default, the other form follows
    ("8080", 8080, '*8080 | "8080"'),
    ("8080", None, '*"8080" | 8080'),
    ("1.10", None, '*"1.10" | 1.10'),
    ("1.5", 1.5, '*1.5 | "1.5"'),
    ("1e-05", 1e-05, '*1e-05 | "1e-05"'),
    ("true", True, '*true | "true"'),
    ("false", False, '*false | "false"'),
    ("TRUE", None, '*"TRUE" | true'),
    # no other reading
    ("0123", None, '"0123"'), ("checkout", None, '"checkout"'), ("", None, '""'), ("yes", None, '"yes"'),
])
def test_value_expr(text, own, expected):
    assert readings.value_expr(text, own) == expected


def test_a_null_reads_as_null_or_empty_text_and_an_empty_text_as_empty_text_or_null():
    assert readings.data_for_cue({"a": ""}, {"a": None}) == '"a": *null | ""\n'      # the file's own form is the default
    assert readings.data_for_cue({"a": ""}, {"a": ""}) == '"a": *"" | null\n'
    assert readings.data_for_cue({"a": ""}) == '"a": *"" | null\n'                   # a .env file: no own form given
    assert readings.data_for_cue({"a": " "}) == '"a": " "\n'                          # only the empty text is null


def test_keys_and_values_that_need_escaping():
    data = readings.data_for_cue({'a"b': 'say "hi"\n', "c\\(d)": "\\(x)", "e": "tab\t😀"})
    assert '"a\\"b": "say \\"hi\\"\\n"' in data
    assert '"c\\\\(d)": "\\\\(x)"' in data          # a backslash is escaped, so \( is never an interpolation
    assert '"tab\\t\\U0001f600"' in data


def test_the_data_file_names_no_package_and_has_one_line_per_key():
    data = readings.data_for_cue({"a": "1", "b": "x", 'c"d': "true"})
    assert not data.startswith("package")
    assert data.splitlines() == ['"a": *"1" | 1', '"b": "x"', '"c\\"d": *"true" | true']


def test_the_gate_is_a_parser_not_a_pattern():
    import inspect

    source = inspect.getsource(readings)
    assert "re.compile" not in source and "import re" not in source


# ── the file's own form ───────────────────────────────────────────────────────

def own(path: str, text: str, variables_map: dict | None = None):
    parsed = load(text, path)
    flat = transformer.transform(content=text, file_path=path, variables_map=variables_map or {}, env="", root_key="",
                                 parsed=parsed if isinstance(parsed, (dict, list)) else None)
    return flat, transformer.own_forms(parsed if isinstance(parsed, (dict, list)) else None, flat, variables_map)


def test_yaml_json_and_toml_keep_the_types_the_file_wrote():
    for path, text in [("a.yaml", "port: 8080\nssl: true\nratio: 0.5\nname: x\nquoted: \"8080\"\n"),
                       ("a.json", '{"port": 8080, "ssl": true, "ratio": 0.5, "name": "x", "quoted": "8080"}'),
                       ("a.toml", 'port = 8080\nssl = true\nratio = 0.5\nname = "x"\nquoted = "8080"\n')]:
        flat, forms = own(path, text)
        assert forms["port"] == 8080 and isinstance(forms["port"], int) and not isinstance(forms["port"], bool), path
        assert forms["ssl"] is True and forms["ratio"] == 0.5 and forms["name"] == "x", path
        assert forms["quoted"] == "8080", path
        assert flat["port"] == "8080" and flat["ssl"] == "true", path      # what a sink gets does not change


def test_a_nested_key_and_a_list_item_are_typed_too():
    flat, forms = own("a.yaml", "db:\n  port: 5432\nhosts:\n  - 1\n  - two\n")
    assert forms == {"db.port": 5432, "hosts.0": 1, "hosts.1": "two"}
    assert flat == {"db.port": "5432", "hosts.0": "1", "hosts.1": "two"}


def test_text_only_formats_stay_text():
    for path, text in [("a.env", "PORT=8080\nDEBUG=true\n"), ("a.ini", "[db]\nport = 8080\ndebug = true\n")]:
        flat, forms = own(path, text)
        assert all(isinstance(v, str) for v in forms.values()), path
        assert forms == flat, path


def test_a_renamed_key_keeps_its_value_type():
    flat, forms = own("a.yaml", "db:\n  port: 5432\n", {"DB_PORT": "db.port"})
    assert forms == {"DB_PORT": 5432} and flat == {"DB_PORT": "5432"}


def test_the_typed_view_changes_nothing_a_sink_writes():
    """own_forms only reads: the flat text and the file's tree (file_shape, which MongoDB and S3 store) are what they were."""
    import copy

    parsed = {"db": {"port": 5432, "ssl": True, "hosts": ["a", 1]}, "ratio": 0.5}
    flat = {"db.port": "5432", "db.ssl": "true", "db.hosts.0": "a", "db.hosts.1": "1", "ratio": "0.5"}
    parsed_before, flat_before = copy.deepcopy(parsed), dict(flat)
    shape_before = transformer.file_shape(parsed, flat, {})
    transformer.own_forms(parsed, flat, {})
    assert parsed == parsed_before and flat == flat_before
    assert transformer.file_shape(parsed, flat, {}) == shape_before
    assert shape_before["db"]["port"] == 5432 and shape_before["db"]["ssl"] is True      # a sink still stores the int and the bool


def test_a_value_varTrack_changed_is_the_text_it_now_holds():
    parsed = {"db": {"password": "@secret(secret/x#p)", "port": 5432}}
    flat = {"db.password": "s3cret", "db.port": "5432"}      # the secret was resolved
    assert transformer.own_forms(parsed, flat) == {"db.password": "s3cret", "db.port": 5432}


# ── what a schema can say (real cue) ─────────────────────────────────────────

# Where `cue` is missing a developer's run skips these. In CI (CI is set) they do not skip: without it they fail,
# so a runner that lost the program cannot pass the schema tests by not running them.
needs_cue = pytest.mark.skipif(shutil.which("cue") is None and not os.environ.get("CI"),
                               reason="the cue program is not installed")


def vet(tmp_path: Path, schema: str, path: str, text: str, strict: bool = True):
    """Run a file through the same parse, flatten and Validator a sync uses. Returns None, or CUE's error."""
    (tmp_path / "app.cue").write_text(schema, encoding="utf-8")
    parsed = load(text, path)
    flat = transformer.transform(content=text, file_path=path, variables_map={}, env="", root_key="",
                                 parsed=parsed if isinstance(parsed, (dict, list)) else None)
    try:
        Validator(schema_dir=tmp_path).validate(flat, Path(path).name, strict=strict, schema_path=tmp_path / "app.cue",
                                                own=transformer.own_forms(parsed if isinstance(parsed, (dict, list)) else None, flat))
    except CUEValidationError as exc:
        return str(exc)
    return None


@needs_cue
@pytest.mark.parametrize("schema, path, text, ok", [
    # an int, however the file wrote it
    ('"port": int & >=1024 & <=65535', "a.yaml", "port: 8080\n", True),
    ('"port": int & >=1024 & <=65535', "a.yaml", 'port: "8080"\n', True),
    ('"port": int & >=1024 & <=65535', "a.json", '{"port": 8080}', True),
    ('"port": int & >=1024 & <=65535', "a.toml", "port = 8080\n", True),
    ('"PORT": int & >=1024 & <=65535', "a.env", "PORT=8080\n", True),
    ('"db.port": int & >=1024', "a.ini", "[db]\nport = 8080\n", True),
    ('"port": int & >=9000', "a.yaml", "port: 8080\n", False),
    ('"port": uint16', "a.yaml", "port: 70000\n", False),
    ('"port": int', "a.yaml", "port: 80a\n", False),
    ('"port": int', "a.yaml", "port: abc\n", False),
    # today's schemas keep passing
    ('"port": string', "a.yaml", "port: 8080\n", True),
    ('"port": string', "a.yaml", 'port: "8080"\n', True),
    ('"port": string & =~"^[0-9]+$"', "a.yaml", "port: 8080\n", True),
    ('"port": string & =~"^[0-9]+$"', "a.env", "port=8080\n", True),
    ('"name": string & !=""', "a.yaml", "name: checkout\n", True),
    # numbers that are not integers, and text that is not a number
    ('"v": number', "a.yaml", 'v: "1.10"\n', True),
    ('"v": int', "a.yaml", 'v: "1.10"\n', False),
    ('"v": float & >0.5', "a.yaml", "v: 0.75\n", True),
    ('"v": number', "a.env", "v=1e5\n", True),                   # JSON says 1e5 is a number; in CUE it is a float
    ('"v": int', "a.env", "v=1e5\n", False),
    ('"x": int', "a.env", "x=0x1F\n", False),                    # CUE would read 0x1F as 31; the gate does not
    ('"x": int', "a.env", "x=1_000\n", False),
    ('"x": int', "a.env", "x=+5\n", False),
    ('"zip": int', "a.yaml", 'zip: "0123"\n', False),
    ('"zip": string & =~"^[0-9]{4}$"', "a.yaml", 'zip: "0123"\n', True),
    ('"name": int', "a.yaml", "name: checkout\n", False),
    # booleans
    ('"debug": bool', "a.yaml", "debug: true\n", True),
    ('"debug": bool', "a.env", "DEBUG=TRUE\n".replace("DEBUG", "debug"), True),
    ('"debug": bool', "a.yaml", "debug: yes\n", True),           # YAML 1.1 reads yes as a boolean
    ('"debug": bool', "a.env", "debug=yes\n", False),             # in a text file, yes is just text
    ('"debug": bool', "a.yaml", "debug: maybe\n", False),
    # either form
    ('"t": int | =~"^[0-9]+s$"', "a.yaml", 'T: 1\n'.replace("T", "t"), True),
    ('"t": int | =~"^[0-9]+s$"', "a.yaml", 't: 30s\n', True),
    ('"t": int | =~"^[0-9]+s$"', "a.yaml", 't: soon\n', False),
    # one of a set
    ('"p": 80 | 443', "a.yaml", "p: 443\n", True),
    ('"p": 80 | 443', "a.yaml", "p: 8080\n", False),
    # a missing field and an optional one
    ('"a": int', "a.yaml", "b: 1\n", False),
    ('"a"?: int', "a.yaml", "b: 1\n", True),
    # an empty value is text, or null: "a number, or left empty" is `int | null`
    ('"PORT": int | null', "a.env", "PORT=\n", True),
    ('"PORT": int | null', "a.env", "PORT=8080\n", True),
    ('"PORT": int | null', "a.env", "PORT=abc\n", False),
    ('"PORT": int', "a.env", "PORT=\n", False),
    ('"PORT": string', "a.env", "PORT=\n", True),
    ('"PORT": string & !=""', "a.env", "PORT=\n", False),
    ('"port": int | null', "a.yaml", 'port: ""\n', True),
    ('"port": int | null', "a.yaml", "port:\n", True),
    ('"port": int', "a.yaml", "port:\n", False),
    ('"port": int | null', "a.yaml", 'port: " "\n', False),    # a space is not empty
])
def test_the_schema_table(tmp_path, schema, path, text, ok):
    error = vet(tmp_path, schema + "\n", path, text)
    assert (error is None) == ok, error


@needs_cue
def test_a_schema_with_a_package_clause_still_checks(tmp_path):
    schema = 'package schema\n\n"port": int & >=1\n'
    assert vet(tmp_path, schema, "a.yaml", "port: 80\n") is None
    assert vet(tmp_path, schema, "a.yaml", "port: 0\n") is not None


@needs_cue
def test_the_error_names_the_value_as_the_file_wrote_it(tmp_path):
    error = vet(tmp_path, '"DATABASE_PORT": int & >=6000\n', "app.env", "DATABASE_PORT=5432\n")
    assert error.splitlines()[0] == 'CUE validation failed for app.env: DATABASE_PORT = "5432" in app.env'
    assert "out of bound >=6000" in error


@needs_cue
def test_the_error_for_a_dotted_key(tmp_path):
    error = vet(tmp_path, '"server.port": int & >=9000\n', "a.json", '{"server": {"port": 8080}}')
    assert error.splitlines()[0] == 'CUE validation failed for a.json: server.port = "8080" in a.json'


@needs_cue
def test_the_error_prefix_names_each_failing_key_exactly(tmp_path):
    """The keys come from CUE's positions in the data file: a key with quotes, dots, a backslash and a non-ASCII letter is exact."""
    schema = '"a.b": int & >=10\n"say \\"hi\\"": int\n"back\\\\slash": int\n"שלום": int\n"fine": int\n'
    text = ('{"a": {"b": 1}, "say \\"hi\\"": "x", "back\\\\slash": "y", "שלום": "z", "fine": 5}')
    error = vet(tmp_path, schema, "a.json", text)
    first = error.splitlines()[0]
    assert first == 'CUE validation failed for a.json: a.b = "1" in a.json'
    assert 'say "hi" = "x" in a.json' in error
    assert 'back\\slash = "y" in a.json' in error
    assert 'שלום = "z" in a.json' in error
    assert 'fine = "5"' not in error                      # a key that passed is not named


@needs_cue
def test_a_missing_key_is_named_by_cue_and_adds_no_prefix(tmp_path):
    error = vet(tmp_path, '"app.name": string & !=""\n"x": int\n', "a.yaml", "x: 1\n")
    assert error.startswith('CUE validation failed for a.yaml: "app.name": incomplete value')
    assert " = " not in error.splitlines()[0]


@needs_cue
def test_the_prefix_is_capped_and_says_how_many_more(tmp_path):
    schema = "".join(f'"k{i}": int\n' for i in range(8))
    text = "".join(f"k{i}: x\n" for i in range(8))
    error = vet(tmp_path, schema, "a.yaml", text)
    assert error.count(' = "x" in a.yaml') == 5 and "… and 3 more" in error


@needs_cue
def test_a_value_the_schema_does_not_mention_is_not_an_error(tmp_path):
    assert vet(tmp_path, '"a": int\n', "a.yaml", "a: 1\nb: whatever\nc: [1, 2]\n") is None


@needs_cue
def test_the_old_flat_json_form_is_not_what_cue_sees_any_more(tmp_path):
    """examples/schemas/annotated_schema.cue-style types, which no file could pass before."""
    schema = '"app_port": int & >=1024 & <=65535\n"feature.dark_mode": bool\n"database.max_connections": int & >=1 & <=1000\n'
    text = '{"app_port": 8080, "feature": {"dark_mode": false}, "database": {"max_connections": "500"}}'
    assert vet(tmp_path, schema, "settings.json", text) is None
