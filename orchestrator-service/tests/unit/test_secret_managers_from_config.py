"""
@secret() fields read Vault through config.cue's secret_managers, and the
secret's path and key are taken from an "@secret(mount/path#key)" value when
the file gives one, else from the manager's path_prefix and the field's name.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pipeline.secret_resolver import effective_secrets, resolve_secrets, secret_managers_from_bundle

BUNDLE = {"secret_managers": [
    {"vault": {"tag": "default", "endpoint": "http://vault:8200", "mount_point": "secret", "kv_version": 2,
               "token_auth": {"token": "root"}}},
    {"vault": {"tag": "other-vault", "endpoint": "http://vault2:8200", "mount_point": "kv", "kv_version": 1,
               "app_role_auth": {"role_id": "r", "secret_id": "s"}}},
]}


def test_managers_from_config_cue():
    managers = secret_managers_from_bundle(BUNDLE)
    assert set(managers) == {"default", "vault-default", "other-vault", "vault-other-vault"}
    assert managers["default"]["vault_addr"] == "http://vault:8200"
    assert managers["default"]["auth"] == {"token": "root"}
    assert managers["other-vault"]["kv_version"] == 1
    assert managers["other-vault"]["auth"] == {"app_role": {"role_id": "r", "secret_id": "s"}}


def test_the_only_manager_is_the_default():
    managers = secret_managers_from_bundle({"secret_managers": [
        {"vault": {"tag": "prod", "endpoint": "http://v", "token_auth": {"token": "t"}}}]})
    assert managers["default"] is managers["prod"]


def test_a_rules_own_secrets_block_no_longer_counts():
    rule = {"secrets": {"default": {"type": "vault", "vault_addr": "http://rule-vault", "auth": {"token": "x"}}}}
    merged = effective_secrets(rule, BUNDLE)
    assert merged["default"]["vault_addr"] == "http://vault:8200"         # config.cue's
    assert effective_secrets({}, {}) == {}


def test_path_prefix_comes_from_the_managers_entry():
    bundle = {"secret_managers": [{"vault": {
        "endpoint": "http://v", "mount_point": "secret", "path_prefix": "vartrack/configs",
        "token_auth": {"token": "root"}}}]}
    assert secret_managers_from_bundle(bundle)["default"]["path_prefix"] == "vartrack/configs"
    assert secret_managers_from_bundle(BUNDLE)["default"]["path_prefix"] == ""   # none given


def _resolve(flat, fields, config):
    client = MagicMock()
    client.get_secret.return_value = "s3cret"
    with patch("app.pipeline.secret_resolver.VaultClient", return_value=client) as made:
        out = resolve_secrets(flat, fields, config)
    return out, client, made


def test_the_value_names_path_and_key():
    config = effective_secrets({}, BUNDLE)
    out, client, made = _resolve({"db.password": "@secret(secret/myapp/db#password)"}, {"db.password": None}, config)
    assert out["db.password"] == "s3cret"
    client.get_secret.assert_called_once_with("myapp/db", key="password")   # the mount point is not repeated
    assert made.call_args.kwargs["endpoint"] == "http://vault:8200"


def test_ref_picks_the_manager():
    config = effective_secrets({}, BUNDLE)
    _, client, made = _resolve({"api.key": "@secret(app/api#key)"}, {"api.key": "other-vault"}, config)
    client.get_secret.assert_called_once_with("app/api", key="key")
    assert made.call_args.kwargs["endpoint"] == "http://vault2:8200"


def test_a_plain_value_uses_path_prefix_and_the_field_name():
    config = {"default": {"type": "vault", "vault_addr": "http://v", "path_prefix": "myapp/prod", "auth": {"token": "t"}}}
    _, client, _ = _resolve({"db.password": "placeholder"}, {"db.password": None}, config)
    client.get_secret.assert_called_once_with("myapp/prod", key="db.password")

