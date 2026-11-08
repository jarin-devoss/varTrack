"""
A sink is built from its datasource's block in config.cue, parsed with the protos
(app.pipeline.sinks.settings): each field under its own name and type. A rule's copy
of a datasource field is never read.
"""
from __future__ import annotations

import pytest

from app.dependencies import SecretUnavailable
from app.pipeline import datasource_settings as ds
from app.pipeline.sinks.settings import Settings, for_rule, load

# As vartrack-bundle prints it: a credential is {value:}, {file:} or {ref:}, a Duration is "90s".
BUNDLE = {
    "datasources": [
        {"mongo": {"tag": "primary", "endpoint": "mongodb://mongo:27017", "database": "vartrack",
                   "collection": "variables", "update_strategy": "STRATEGY_DOCUMENT"}},
        {"mongo": {"tag": "auth", "host": "db", "port": 27018, "username": "vt", "password": {"value": "p@ss:w"}}},
        {"redis": {"hosts": ["redis:6379"], "database": 2, "password": {"value": "rpw"},
                   "data_structure": "HASH", "ttl": "90s"}},
        {"s3": {"tag": "minio", "bucket": "configs", "region": "us-east-1", "endpoint": "http://minio:9000",
                "access_key_id": {"value": "AKIA"}, "secret_access_key": {"value": "s3cret"}, "prefix": "acme/"}},
        {"linux_server": {"tag": "web", "host": "app-01", "port": 2222, "username": "deploy",
                          "ssh_key": {"private_key": {"file": "/run/secrets/id_rsa"}},
                          "file_paths": ["app.env"], "base_directory": "/etc/myapp", "env_as_path": True}},
        {"zookeeper": {"hosts": ["zk:2181"], "base_path": "/vt", "session_timeout": "15s"}},
        {"configmap": {"tag": "checkout", "namespace": "shop", "configmap_name": "checkout",
                       "insecure_skip_verify": True}},
    ],
}


def settings_for(name):
    return load(name, {}, BUNDLE)


def test_each_field_reaches_the_sink_under_its_own_name_and_type():
    redis = settings_for("redis")
    assert (redis.kind, redis.name) == ("redis", "redis")
    assert redis.get("hosts") == ["redis:6379"] and redis.get("database") == 2
    assert redis.secret("password") == "rpw" and redis.get("data_structure") == "HASH"
    assert redis.seconds("ttl") == 90

    s3 = settings_for("s3-minio")
    assert s3.get("bucket") == "configs" and s3.get("prefix") == "acme/"
    assert s3.secret("access_key_id") == "AKIA" and s3.secret("secret_access_key") == "s3cret"

    zk = settings_for("zookeeper")
    assert zk.get("hosts") == ["zk:2181"] and zk.get("base_path") == "/vt" and zk.seconds("session_timeout") == 15

    cm = settings_for("configmap-checkout")
    assert cm.get("namespace") == "shop" and cm.get("insecure_skip_verify") is True


def test_what_a_sink_derives_from_its_settings():
    from app.pipeline.sinks.linux_server import LinuxServerSink, _key_file
    from app.pipeline.sinks.mongo import MongoSink

    assert MongoSink.uri_of(settings_for("mongo-primary")) == "mongodb://mongo:27017"
    # a credential is escaped in the URI
    assert MongoSink.uri_of(settings_for("mongo-auth")) == "mongodb://vt:p%40ss%3Aw@db:27018"
    web = settings_for("linux_server-web")
    assert LinuxServerSink.file_path_of(web) == "/etc/myapp/app.env"
    assert _key_file(web) == "/run/secrets/id_rsa"


def test_set_means_written_in_config_cue_so_an_explicit_false_is_not_a_default():
    s = Settings.of("configmap", env_as_name_suffix=False)
    assert s.has("env_as_name_suffix") and s.get("env_as_name_suffix", True) is False
    assert not s.has("env_as_namespace") and s.get("env_as_namespace", "unset") == "unset"
    # an enum left at *_UNSPECIFIED counts as unset
    assert Settings.of("mongo", update_strategy="STRATEGY_UNSPECIFIED").get("update_strategy", "d") == "d"


def test_a_field_config_cue_cannot_write_is_refused_when_parsed():
    with pytest.raises(Exception, match="mongo_uri"):
        Settings.parse("mongo", "mongo", {"mongo_uri": "mongodb://x"})
    with pytest.raises(TypeError, match="not a credential"):
        Settings.of("redis").secret("hosts")


def test_secret_from_a_file(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    bundle = {"datasources": [{"redis": {"password": {"file": str(secret)}}}]}
    assert load("redis", {}, bundle).secret("password") == "from-file"


def test_an_unreadable_secret_makes_the_datasource_unusable_not_anonymous():
    bundle = {"datasources": [{"redis": {"password": {"file": "/nonexistent/pw"}}}]}
    with pytest.raises(SecretUnavailable, match="redis: password: cannot read /nonexistent/pw"):
        load("redis", {}, bundle).secret("password")


def test_a_rules_copy_of_a_datasource_field_is_not_read(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: BUNDLE)
    settings = for_rule({"platform": "gitea", "datasource": "mongo-primary", "endpoint": "mongodb://evil",
                         "collection": "evil", "key_format": "dot"})
    assert settings.get("endpoint") == "mongodb://mongo:27017" and settings.get("collection") == "variables"


def test_a_datasource_config_cue_does_not_declare_has_its_defaults(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: BUNDLE)
    unknown = for_rule({"datasource": "mongo-unknown"})
    assert unknown.kind == "mongo" and unknown.name == "mongo-unknown" and unknown.present == frozenset()
    with pytest.raises(ValueError, match="no sink for datasource 'ftp-files'"):
        for_rule({"datasource": "ftp-files"})


def test_create_sink_finds_a_tagged_linux_server(monkeypatch):
    from app.pipeline import sinks
    monkeypatch.setattr(ds, "_bundle", lambda: BUNDLE)
    built = {}

    class Fake:
        def __init__(self, settings, rule_config, tenant_id):
            built.update(settings=settings, rule_config=rule_config, tenant_id=tenant_id)

    monkeypatch.setitem(sinks.SINK_REGISTRY, "linux_server", Fake)
    sinks.create_sink("linux_server-web", {"key_format": "dot"}, "acme")
    assert built["settings"].get("host") == "app-01" and built["settings"].name == "linux_server-web"
    assert built["rule_config"] == {"key_format": "dot"} and built["tenant_id"] == "acme"
    with pytest.raises(ValueError, match="No sink registered"):
        sinks.create_sink("ftp-files", {}, "acme")


def test_the_rule_names_the_secret_manager(monkeypatch):
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
    settings = for_rule({"platform": "gitea", "datasource": "redis", "secret_manager": "vault-b"})
    assert settings.secret("password") == "redis/main#password" and asked["manager"] == "vault-b"

    # no rule names one, several managers, none tagged default: the datasource cannot be used
    with pytest.raises(SecretUnavailable, match='or tag one "default"'):
        for_rule({"platform": "gitea", "datasource": "redis"}).secret("password")

    # a name that is not in secret_managers
    with pytest.raises(SecretUnavailable, match="'vault-c' is not in the bundle's secret_managers"):
        for_rule({"platform": "x", "datasource": "redis", "secret_manager": "vault-c"}).secret("password")


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
    assert load("redis", {}, bundle).secret("password") == "v"
    assert asked["manager"] == "vault-default"


def test_secret_annotations_use_the_rule_s_secret_manager():
    from app.pipeline.secret_resolver import effective_secrets
    bundle = {"secret_managers": [
        {"vault": {"tag": "default", "endpoint": "http://v1", "token_auth": {"token": "t"}}},
        {"vault": {"tag": "prod", "endpoint": "http://v2", "token_auth": {"token": "t"}}}]}
    assert effective_secrets({}, bundle)["default"]["vault_addr"] == "http://v1"
    assert effective_secrets({"secret_manager": "vault-prod"}, bundle)["default"]["vault_addr"] == "http://v2"


def test_the_mongo_sink_gives_the_driver_the_connect_timeout_it_was_set(monkeypatch):
    """A stopped server must fail a read within the datasource's own timeout, not after the driver's 20 s + 30 s."""
    import pymongo

    from app.pipeline.sinks.mongo import MongoSink
    from app.pipeline.sinks.settings import Settings

    seen = {}

    class _Client:
        def __init__(self, uri, **options):
            seen.update(options)

        def __getitem__(self, name):
            return {}

    monkeypatch.setattr(pymongo, "MongoClient", _Client)
    monkeypatch.setattr(MongoSink, "_ensure_indexes", lambda self: None, raising=False)
    try:
        MongoSink(settings=Settings.of("mongo", connect_timeout_ms=1500, endpoint="mongodb://mongo:27017"), rule_config={})
    except Exception:
        pass  # what the sink does after the client is built is not this test's business
    assert seen.get("connectTimeoutMS") == 1500 and seen.get("serverSelectionTimeoutMS") == 1500

    seen.clear()
    try:
        MongoSink(settings=Settings.of("mongo", endpoint="mongodb://mongo:27017"), rule_config={})
    except Exception:
        pass
    assert "connectTimeoutMS" not in seen and "serverSelectionTimeoutMS" not in seen   # 0: the driver's own waits


def test_the_redis_sink_gives_the_client_the_timeouts_it_was_set(monkeypatch):
    """A Redis that hangs must fail a sync after the datasource's read_timeout, not leave the worker waiting for ever."""
    import redis

    from app.pipeline.sinks.redis import RedisSink
    from app.pipeline.sinks.settings import Settings

    seen = {}

    class _Client:
        def __init__(self, **options):
            seen.update(options)

    monkeypatch.setattr(redis, "Redis", _Client)
    for settings, want in (
        (Settings.of("redis", hosts=["redis:6379"], connection_timeout="3s", read_timeout="7s"), (3.0, 7.0)),
        (Settings.of("redis", hosts=["redis:6379"]), (None, None)),
    ):
        seen.clear()
        try:
            RedisSink(settings=settings, rule_config={})
        except Exception:
            pass
        assert (seen.get("socket_connect_timeout"), seen.get("socket_timeout")) == want
