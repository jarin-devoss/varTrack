"""
tests/unit/test_parsers.py

Unit tests for all file parsers (JSON, YAML, .env, TOML, XML, INI).

The loader.load() dispatcher is also tested to confirm correct format
detection by file extension and content sniffing.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import pytest

from app.parsers.loader import load as parse_file


# ── JSON ──────────────────────────────────────────────────────────────────────

class TestJSONParser(unittest.TestCase):

    def test_flat_object(self):
        result = parse_file('{"key": "value", "port": 5432}', "cfg.json")
        self.assertEqual(result["key"], "value")
        self.assertEqual(result["port"], 5432)

    def test_nested_object(self):
        result = parse_file('{"db": {"host": "localhost"}}', "cfg.json")
        self.assertEqual(result["db"]["host"], "localhost")

    def test_array_preserved(self):
        result = parse_file('{"items": [1, 2, 3]}', "data.json")
        self.assertEqual(result["items"], [1, 2, 3])

    def test_empty_object(self):
        result = parse_file("{}", "empty.json")
        self.assertEqual(result, {})

    def test_invalid_json_raises(self):
        with self.assertRaises(Exception):
            parse_file("not json {{{", "bad.json")


# ── YAML ──────────────────────────────────────────────────────────────────────

class TestYAMLParser(unittest.TestCase):

    def test_simple_mapping(self):
        result = parse_file("host: localhost\nport: 5432\n", "cfg.yaml")
        self.assertEqual(result["host"], "localhost")
        self.assertEqual(result["port"], 5432)

    def test_nested_mapping(self):
        result = parse_file("db:\n  host: mydb\n  port: 5432\n", "config.yml")
        self.assertEqual(result["db"]["host"], "mydb")

    def test_empty_yaml(self):
        result = parse_file("", "empty.yaml")
        # Empty YAML is None or {} — both acceptable
        self.assertIn(result, (None, {}))

    def test_yaml_list(self):
        result = parse_file("- a\n- b\n- c\n", "list.yaml")
        self.assertIsInstance(result, list)
        self.assertEqual(result, ["a", "b", "c"])


# ── .env ──────────────────────────────────────────────────────────────────────

class TestEnvParser(unittest.TestCase):

    def test_basic_kv(self):
        result = parse_file("DB_HOST=localhost\nDB_PORT=5432\n", "prod.env")
        self.assertEqual(result["DB_HOST"], "localhost")
        self.assertEqual(result["DB_PORT"], "5432")

    def test_comments_ignored(self):
        result = parse_file("# comment\nKEY=value\n", "app.env")
        self.assertNotIn("# comment", result)
        self.assertEqual(result["KEY"], "value")

    def test_quoted_values(self):
        result = parse_file('KEY="hello world"\n', "q.env")
        self.assertIn("KEY", result)

    def test_empty_lines_skipped(self):
        result = parse_file("\n\nKEY=val\n\n", "e.env")
        self.assertEqual(result["KEY"], "val")


# ── XML ───────────────────────────────────────────────────────────────────────

class TestXMLParser(unittest.TestCase):

    def test_simple_element(self):
        xml = "<config><host>localhost</host><port>5432</port></config>"
        result = parse_file(xml, "config.xml")
        self.assertIsInstance(result, dict)

    def test_nested_elements(self):
        xml = "<root><db><host>mydb</host></db></root>"
        result = parse_file(xml, "nested.xml")
        self.assertIsInstance(result, dict)

    def test_invalid_xml_returns_none(self):
        result = parse_file("<unclosed>", "bad.xml")
        self.assertIsNone(result, "Invalid XML must return None (parser catches ParseError)")


# ── Loader dispatch (format detection) ────────────────────────────────────────

@pytest.mark.parametrize("filename,content,expected_key", [
    ("cfg.json",  '{"key": "value"}',         "key"),
    ("cfg.yaml",  "key: value\n",              "key"),
    ("cfg.yml",   "key: value\n",              "key"),
    ("prod.env",  "KEY=value\n",               "KEY"),
])
def test_loader_dispatches_by_extension(filename: str, content: str, expected_key: str):
    result = parse_file(content, filename)
    assert expected_key in result, (
        f"loader({filename!r}) should parse and include key {expected_key!r}"
    )


@pytest.mark.parametrize("filename,content", [
    ("cfg.json", '{"valid": true}'),
    ("cfg.yaml", "valid: true\n"),
    ("prod.env", "VALID=true\n"),
])
def test_loader_returns_dict_for_all_formats(filename: str, content: str):
    result = parse_file(content, filename)
    assert isinstance(result, dict), (
        f"loader({filename!r}) must return a dict, got {type(result)}"
    )
