"""
tests/unit/test_transformer.py

Unit tests for app/pipeline/transformer.transform() and
app/pipeline/env_resolver.resolve() (pure-Python, no Rust extension needed).
"""
from __future__ import annotations

import unittest

from app.pipeline.transformer import transform
from app.pipeline.env_resolver import resolve


# ── transform() tests ─────────────────────────────────────────────────────────

class TestTransform(unittest.TestCase):

    def _t(self, content: str, file_path: str, **kwargs) -> dict:
        """Thin helper: call transform() with required keyword args."""
        return transform(
            content=content,
            file_path=file_path,
            variables_map=kwargs.pop("variables_map", {}),
            env=kwargs.pop("env", "test"),
            **kwargs,
        )

    def test_flat_json(self):
        result = self._t('{"a": 1, "b": 2}', "file.json")
        self.assertEqual(result["a"], "1")
        self.assertEqual(result["b"], "2")

    def test_nested_json(self):
        result = self._t('{"db": {"host": "localhost", "port": 5432}}', "config.json")
        self.assertEqual(result["db.host"], "localhost")
        self.assertEqual(result["db.port"], "5432")

    def test_array_flattening(self):
        result = self._t('{"items": [10, 20, 30]}', "data.json")
        self.assertEqual(result["items.0"], "10")
        self.assertEqual(result["items.2"], "30")

    def test_yaml_parsing(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        content = "db:\n  host: mydb\n  port: 5432\n"
        result = self._t(content, "config.yaml")
        self.assertEqual(result["db.host"], "mydb")

    def test_env_file_parsing(self):
        content = "# comment\nDB_HOST=localhost\nDB_PORT=5432\n"
        result = self._t(content, "prod.env")
        self.assertEqual(result["DB_HOST"], "localhost")
        self.assertEqual(result["DB_PORT"], "5432")

    def test_variables_map_override(self):
        content = '{"database.host": "localhost"}'
        result = self._t(
            content, "f.json",
            variables_map={"DB_HOST": "database.host"},
        )
        self.assertEqual(result["DB_HOST"], "localhost")
        self.assertNotIn("database.host", result)

    def test_invalid_content_returns_empty(self):
        result = self._t("NOT_JSON_OR_YAML{{{{", "broken.json")
        self.assertEqual(result, {})


# ── resolve() tests ───────────────────────────────────────────────────────────

class TestEnvResolve(unittest.TestCase):

    def test_branch_map_priority(self):
        env = resolve(
            branch="main",
            branch_map={"main": "production"},
            env_as_branch=True,
        )
        self.assertEqual(env, "production")  # branch_map wins over env_as_branch

    def test_env_as_branch_fallback(self):
        env = resolve(branch="staging", env_as_branch=True)
        self.assertEqual(env, "staging")

    def test_env_as_pr(self):
        env = resolve(pr_number="42", env_as_pr=True)
        self.assertEqual(env, "pr-42")

    def test_file_path_map(self):
        env = resolve(
            file_path="config/prod/values.yaml",
            file_path_map={"config/prod/values.yaml": "prod"},
        )
        self.assertEqual(env, "prod")

    def test_default_fallback(self):
        env = resolve()
        self.assertEqual(env, "default")

    def test_tag_env(self):
        env = resolve(tag="v1.2.3", env_as_tags=True)
        self.assertEqual(env, "v1.2.3")


if __name__ == "__main__":
    unittest.main()
