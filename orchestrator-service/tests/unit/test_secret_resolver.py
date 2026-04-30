"""
tests/unit/test_secret_resolver.py

Unit tests for app/pipeline/secret_resolver.py.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

for _m in ("vartrack_core", "pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

from app.pipeline.secret_resolver import (
    mask_secrets,
    parse_secret_fields,
    resolve_secrets,
)

_SECRETS_CONFIG = {
    "default": {
        "type": "vault",
        "vault_addr": "https://vault.example.com",
        "auth": {"type": "token", "token": "s.test"},
        "mount_point": "secret",
        "path_prefix": "myapp/prod",
    },
    "bla": {
        "type": "vault",
        "vault_addr": "https://other-vault.example.com",
        "auth": {"type": "token", "token": "s.other"},
        "mount_point": "kv",
        "path_prefix": "other/prod",
    },
}


def _schema(content: str) -> Path:
    tmpdir = tempfile.mkdtemp()
    p = Path(tmpdir) / "config.yaml.cue"
    p.write_text(content, encoding="utf-8")
    return p


class TestParseSecretFields(unittest.TestCase):

    def test_no_annotations_returns_empty(self):
        schema = _schema('{\n  "host": string\n  "port": int\n}')
        self.assertEqual(parse_secret_fields(schema), {})

    def test_at_secret_default(self):
        schema = _schema('"db.password": string @secret()')
        result = parse_secret_fields(schema)
        self.assertIn("db.password", result)
        self.assertIsNone(result["db.password"])

    def test_at_secret_with_ref(self):
        schema = _schema('"api_key": string @secret(ref="bla")')
        result = parse_secret_fields(schema)
        self.assertEqual(result.get("api_key"), "bla")

    def test_multiple_annotations(self):
        schema = _schema(
            '"db.password": string @secret()\n'
            '"api_key":     string @secret(ref="bla")\n'
            '"host":        string\n'
        )
        result = parse_secret_fields(schema)
        self.assertIn("db.password", result)
        self.assertIn("api_key", result)
        self.assertNotIn("host", result)
        self.assertIsNone(result["db.password"])
        self.assertEqual(result["api_key"], "bla")

    def test_nonexistent_schema_returns_empty(self):
        self.assertEqual(parse_secret_fields(Path("/nonexistent/schema.cue")), {})

    def test_none_schema_returns_empty(self):
        self.assertEqual(parse_secret_fields(None), {})


class TestMaskSecrets(unittest.TestCase):

    def test_masks_annotated_fields(self):
        flat = {"db.password": "supersecret", "host": "localhost"}
        result = mask_secrets(flat, {"db.password": None})
        self.assertEqual(result["db.password"], "***")
        self.assertEqual(result["host"], "localhost")

    def test_original_dict_not_mutated(self):
        flat = {"db.password": "supersecret"}
        mask_secrets(flat, {"db.password": None})
        self.assertEqual(flat["db.password"], "supersecret")

    def test_no_secret_fields_returns_unchanged(self):
        flat = {"host": "localhost", "port": "5432"}
        self.assertEqual(mask_secrets(flat, {}), flat)

    def test_missing_flat_key_ignored(self):
        flat = {"host": "localhost"}
        result = mask_secrets(flat, {"db.password": None})
        self.assertNotIn("db.password", result)
        self.assertEqual(result["host"], "localhost")


class TestResolveSecrets(unittest.TestCase):

    def _vault_mock(self, return_value: str = "vault-secret-value"):
        mock_client = MagicMock()
        mock_client.get_secret.return_value = return_value
        mock_cls = MagicMock(return_value=mock_client)
        return mock_cls, mock_client

    def test_injects_default_secret_manager(self):
        flat = {"db.password": "placeholder", "host": "localhost"}
        mock_cls, mock_client = self._vault_mock("real-password")
        with patch("app.pipeline.secret_resolver.VaultClient", mock_cls):
            result = resolve_secrets(flat, {"db.password": None}, _SECRETS_CONFIG)
        self.assertEqual(result["db.password"], "real-password")
        self.assertEqual(result["host"], "localhost")
        mock_client.get_secret.assert_called_once_with(
            _SECRETS_CONFIG["default"]["path_prefix"],
            key="db.password",
        )

    def test_injects_named_secret_manager(self):
        flat = {"api_key": "placeholder"}
        mock_cls, mock_client = self._vault_mock("bla-api-key")
        with patch("app.pipeline.secret_resolver.VaultClient", mock_cls):
            result = resolve_secrets(flat, {"api_key": "bla"}, _SECRETS_CONFIG)
        self.assertEqual(result["api_key"], "bla-api-key")
        call_kwargs = mock_cls.call_args.kwargs
        self.assertEqual(call_kwargs["endpoint"], _SECRETS_CONFIG["bla"]["vault_addr"])

    def test_field_absent_from_flat_data_skipped(self):
        flat = {"host": "localhost"}
        mock_cls, mock_client = self._vault_mock()
        with patch("app.pipeline.secret_resolver.VaultClient", mock_cls):
            result = resolve_secrets(flat, {"db.password": None}, _SECRETS_CONFIG)
        mock_client.get_secret.assert_not_called()
        self.assertEqual(result, flat)

    def test_missing_sm_config_no_crash(self):
        flat = {"api_key": "placeholder"}
        with patch("app.pipeline.secret_resolver.VaultClient"):
            result = resolve_secrets(flat, {"api_key": "nonexistent_sm"}, _SECRETS_CONFIG)
        self.assertEqual(result["api_key"], "placeholder")

    def test_vault_failure_no_crash(self):
        flat = {"db.password": "placeholder"}
        mock_cls = MagicMock()
        mock_client = MagicMock()
        mock_client.get_secret.side_effect = RuntimeError("vault unreachable")
        mock_cls.return_value = mock_client
        with patch("app.pipeline.secret_resolver.VaultClient", mock_cls):
            result = resolve_secrets(flat, {"db.password": None}, _SECRETS_CONFIG)
        self.assertEqual(result["db.password"], "placeholder")

    def test_empty_secret_fields_returns_unchanged(self):
        flat = {"host": "localhost"}
        self.assertEqual(resolve_secrets(flat, {}, _SECRETS_CONFIG), flat)

    def test_empty_secrets_config_returns_unchanged(self):
        flat = {"db.password": "placeholder"}
        result = resolve_secrets(flat, {"db.password": None}, {})
        self.assertEqual(result, flat)


if __name__ == "__main__":
    unittest.main()
