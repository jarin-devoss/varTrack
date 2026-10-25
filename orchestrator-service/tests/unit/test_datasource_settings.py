"""
Sinks take their connection settings from the datasource's block in
config.cue (app.pipeline.datasource_settings), over the rule's copies.
"""
from __future__ import annotations

import pytest

from app.pipeline import datasource_settings as ds
from app.pipeline.datasource_settings import find_datasource, sink_settings, with_datasource_settings

BUNDLE = {
    "datasources": [
        {"mongo": {"tag": "primary", "endpoint": "mongodb://mongo:27017", "database": "vartrack",
                   "collection": "variables", "update_strategy": "STRATEGY_DOCUMENT"}},
        {"mongo": {"tag": "auth", "host": "db", "port": 27018, "username": "vt", "password": "pw"}},
        {"redis": {"hosts": ["redis:6379"], "database": 2, "password": {"value": "rpw"},
                   "data_structure": "HASH", "ttl": "1m30s"}},
        {"s3": {"tag": "minio", "bucket": "configs", "region": "us-east-1", "endpoint": "http://minio:9000",
                "access_key_id": "AKIA", "secret_access_key": {"value": "s3cret"}, "prefix": "acme/"}},
        {"linux_server": {"tag": "web", "host": "app-01", "port": 2222, "username": "deploy",
                          "ssh_key": {"private_key": {"file": "/run/secrets/id_rsa"}},
                          "file_paths": ["app.env"], "base_directory": "/etc/myapp", "env_as_path": True}},
        {"zookeeper": {"hosts": ["zk:2181"], "base_path": "/vt", "session_timeout": "15s"}},
        {"configmap": {"tag": "checkout", "namespace": "shop", "configmap_name": "checkout",
                       "insecure_skip_verify": True}},
        {"vercel": {"token": "vtok", "project_id": "prj_1", "targets": ["production"]}},
    ],
}


def settings_for(name):
    kind, cfg = find_datasource(BUNDLE, name)
    return sink_settings(kind, cfg, name, BUNDLE)


@pytest.mark.parametrize("name,want", [
    ("mongo-primary", {"mongo_uri": "mongodb://mongo:27017", "database": "vartrack", "collection": "variables",
                       "update_strategy": "STRATEGY_DOCUMENT"}),
    ("mongo-auth", {"mongo_uri": "mongodb://vt:pw@db:27018"}),
    ("redis", {"redis_hosts": ["redis:6379"], "redis_db": 2, "redis_password": "rpw",
               "redis_data_structure": "HASH", "redis_ttl_seconds": 90}),
    ("s3-minio", {"s3_bucket": "configs", "s3_region": "us-east-1", "s3_endpoint_url": "http://minio:9000",
                  "s3_access_key_id": "AKIA", "s3_secret_access_key": "s3cret", "s3_key_prefix": "acme/"}),
    ("linux_server-web", {"ssh_host": "app-01", "ssh_port": 2222, "ssh_user": "deploy",
                          "ssh_key_path": "/run/secrets/id_rsa", "file_path": "/etc/myapp/app.env", "env_as_dir": True}),
    ("zookeeper", {"zk_hosts": ["zk:2181"], "zk_base_path": "/vt", "zk_timeout_secs": 15}),
    ("configmap-checkout", {"k8s_namespace": "shop", "k8s_configmap_name": "checkout", "k8s_verify_ssl": False}),
    ("vercel", {"vercel_token": "vtok", "vercel_project_id": "prj_1", "vercel_targets": ["production"]}),
])
def test_sink_settings(name, want):
    assert settings_for(name) == want


def test_secret_from_a_file(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    bundle = {"datasources": [{"redis": {"password": {"file": str(secret)}}}]}
    kind, cfg = find_datasource(bundle, "redis")
    assert sink_settings(kind, cfg, "redis", bundle)["redis_password"] == "from-file"


def test_an_unreadable_secret_leaves_the_setting_to_the_rule():
    bundle = {"datasources": [{"redis": {"password": {"file": "/nonexistent/pw"}}}]}
    kind, cfg = find_datasource(bundle, "redis")
    assert "redis_password" not in sink_settings(kind, cfg, "redis", bundle)


def test_config_cue_wins_and_the_rule_fills_the_rest(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: BUNDLE)
    rule = {"platform": "gitea", "mongo_uri": "mongodb://old:27017", "key_format": "dot", "database": "vartrack"}
    merged = with_datasource_settings("mongo-primary", rule)
    assert merged["mongo_uri"] == "mongodb://mongo:27017"      # config.cue wins
    assert merged["key_format"] == "dot"                        # only in the rule: kept
    assert merged["collection"] == "variables"                  # only in config.cue: added
    assert rule["mongo_uri"] == "mongodb://old:27017"           # the rule itself is not changed


def test_a_datasource_config_cue_does_not_declare_keeps_the_rule(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: BUNDLE)
    rule = {"mongo_uri": "mongodb://x"}
    assert with_datasource_settings("mongo-unknown", rule) == rule
    monkeypatch.setattr(ds, "_bundle", lambda: {})
    assert with_datasource_settings("mongo-primary", rule) == rule


@pytest.mark.parametrize("value,want", [("30s", 30), ("1m30s", 90), ("2h", 7200), (45, 45), ("soon", None)])
def test_seconds(value, want):
    assert ds._seconds(value) == want


def test_create_sink_finds_a_tagged_linux_server(monkeypatch):
    from app.pipeline import sinks
    monkeypatch.setattr(ds, "_bundle", lambda: {})
    built = {}

    class Fake:
        def __init__(self, rule_config, tenant_id):
            built.update(rule_config=rule_config, tenant_id=tenant_id)

    monkeypatch.setitem(sinks.SINK_REGISTRY, "linux_server", Fake)
    sinks.create_sink("linux_server-web", {"ssh_host": "h"}, "acme")
    assert built == {"rule_config": {"ssh_host": "h"}, "tenant_id": "acme"}
    with pytest.raises(ValueError, match="No sink registered"):
        sinks.create_sink("ftp-files", {}, "acme")


def test_the_rule_names_the_secret_manager(caplog, monkeypatch):
    bundle = {
        "secret_managers": [{"vault": {"tag": "a"}}, {"vault": {"tag": "b"}}],
        "datasources": [{"redis": {"password": {"ref": {"path": "redis/main", "key": "password"}}}}],
        "rules": [{"platform": "gitea", "datasource": "redis", "secret_manager": "vault-b"}],
    }
    asked = {}

    class FakeRefs:
        def __init__(self, bundle, name):
            asked["manager"] = name

        def get(self, ref, where):
            return f"{ref['ref']['path']}#{ref['ref']['key']}"

    import app.ui.sessions as sessions
    monkeypatch.setattr(sessions, "_SecretManagerRefs", FakeRefs)
    monkeypatch.setattr(ds, "_bundle", lambda: bundle)

    # the rule (config.cue's, through rule_layers) names vault-b
    merged = with_datasource_settings("redis", {"platform": "gitea", "datasource": "redis", "secret_manager": "vault-b"})
    assert merged["redis_password"] == "redis/main#password" and asked["manager"] == "vault-b"

    # no rule names one, several managers, none tagged default: not resolved
    merged = with_datasource_settings("redis", {"platform": "github", "datasource": "redis"})
    assert "redis_password" not in merged
    assert 'or tag one "default"' in caplog.text

    # a name that is not in secret_managers
    merged = with_datasource_settings("redis", {"platform": "x", "datasource": "redis", "secret_manager": "vault-c"})
    assert "redis_password" not in merged and "'vault-c' is not in the bundle's secret_managers" in caplog.text


def test_without_a_rule_choice_the_one_tagged_default_is_used(monkeypatch):
    bundle = {"secret_managers": [{"vault": {"tag": "prod"}}, {"vault": {"tag": "default"}}],
              "datasources": [{"redis": {"password": {"ref": {"path": "p", "key": "k"}}}}]}
    asked = {}

    class FakeRefs:
        def __init__(self, bundle, name):
            asked["manager"] = name

        def get(self, ref, where):
            return "v"

    import app.ui.sessions as sessions
    monkeypatch.setattr(sessions, "_SecretManagerRefs", FakeRefs)
    kind, cfg = find_datasource(bundle, "redis")
    assert sink_settings(kind, cfg, "redis", bundle)["redis_password"] == "v"
    assert asked["manager"] == "vault-default"


def test_secret_annotations_use_the_rule_s_secret_manager():
    from app.pipeline.secret_resolver import effective_secrets
    bundle = {"secret_managers": [
        {"vault": {"tag": "default", "endpoint": "http://v1", "token_auth": {"token": "t"}}},
        {"vault": {"tag": "prod", "endpoint": "http://v2", "token_auth": {"token": "t"}}}]}
    assert effective_secrets({}, bundle)["default"]["vault_addr"] == "http://v1"
    assert effective_secrets({"secret_manager": "vault-prod"}, bundle)["default"]["vault_addr"] == "http://v2"
