"""
tests/unit/test_env_slice.py
─────────────────────────────
Unit tests for resolve_env_slice / detect_pattern.
"""
from __future__ import annotations
import pytest
from app.pipeline.env_slice import resolve_env_slice, detect_pattern


# ── Pattern 1 ─────────────────────────────────────────────────────────────────

class TestPattern1:
    DATA = {
        "predev":  {"age": 44, "color": "red",  "name": "dan"},
        "default": {"age": 32, "color": "blue"},
    }

    def test_known_env(self):
        result = resolve_env_slice(self.DATA, "predev")
        assert result == {"age": 44, "color": "red", "name": "dan"}

    def test_default_fallback(self):
        result = resolve_env_slice(self.DATA, "staging")
        assert result == {"age": 32, "color": "blue"}

    def test_default_merged_with_env(self):
        # predev has no "color" → should inherit from default
        data = {
            "predev":  {"name": "dan"},
            "default": {"name": "bob", "age": 32},
        }
        result = resolve_env_slice(data, "predev")
        assert result == {"name": "dan", "age": 32}

    def test_detect(self):
        assert detect_pattern(self.DATA, "predev") == "pattern_1"


# ── Pattern 2 ─────────────────────────────────────────────────────────────────

class TestPattern2:
    DATA = {
        "color": {"default": "blue", "predev": "green", "dev": "yellow"},
        "age": 43,
    }

    def test_known_env(self):
        result = resolve_env_slice(self.DATA, "predev")
        assert result == {"color": "green", "age": 43}

    def test_default_fallback(self):
        result = resolve_env_slice(self.DATA, "staging")
        assert result == {"color": "blue", "age": 43}

    def test_scalar_passthrough(self):
        result = resolve_env_slice(self.DATA, "dev")
        assert result["age"] == 43

    def test_detect(self):
        assert detect_pattern(self.DATA, "predev") == "pattern_2"


# ── Pattern 3 ─────────────────────────────────────────────────────────────────

class TestPattern3:
    DATA = {
        "prod":   {"name": "worn"},
        "predev": {"name": "dan"},
        "name":   "bob",
        "age":    33,
    }

    def test_prod_env(self):
        result = resolve_env_slice(self.DATA, "prod")
        assert result == {"name": "worn", "age": 33}

    def test_predev_env(self):
        result = resolve_env_slice(self.DATA, "predev")
        assert result == {"name": "dan", "age": 33}

    def test_unknown_env_returns_scalars(self):
        # No matching env dict and no "default" dict → scalars only
        result = resolve_env_slice(self.DATA, "staging")
        # No env dict matches and no "default" key → pattern not triggered,
        # data returned unchanged
        assert result == self.DATA

    def test_unknown_env_with_default_dict(self):
        data = {
            "prod":    {"name": "worn"},
            "default": {"name": "bob"},
            "age":     33,
        }
        result = resolve_env_slice(data, "staging")
        assert result == {"name": "bob", "age": 33}

    def test_env_overrides_scalar_default(self):
        # "prod" dict should override the scalar "name"
        result = resolve_env_slice(self.DATA, "prod")
        assert result["name"] == "worn"

    def test_detect(self):
        assert detect_pattern(self.DATA, "prod") == "pattern_3"


# ── No pattern ────────────────────────────────────────────────────────────────

class TestNoPattern:
    def test_plain_flat_dict(self):
        data = {"host": "localhost", "port": 5432}
        assert resolve_env_slice(data, "production") == data

    def test_empty_dict(self):
        assert resolve_env_slice({}, "production") == {}

    def test_non_dict_input(self):
        assert resolve_env_slice("not a dict", "production") == "not a dict"  # type: ignore

    def test_detect_none(self):
        assert detect_pattern({"host": "localhost"}, "production") == "none"
