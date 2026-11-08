"""
A tenant writes only to the destinations the operator gave it.

The destination is the resolved place a sync would write (a collection, a key prefix, a
znode path, a file, an object prefix …). It is checked before a sink is opened, and it is what
the sync lease is keyed by, so two tenants writing one place wait for each other.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import tenants
from app.pipeline import destinations, stage_sync
from app.pipeline.models import ETLFile, ETLResult, PayloadContext


# ── where a sync writes ───────────────────────────────────────────────────────

# (datasource, its settings in config.cue, the rule's own options, where it writes)
@pytest.mark.parametrize("datasource, settings, rule, expected", [
    ("redis", {}, {"destination_template": "{tenant}:{env}"}, "pay:production"),
    ("redis", {"key_prefix": "pay:cfg"}, {}, "pay:cfg"),
    ("redis", {}, {}, "pay:redis:production"),
    ("zookeeper", {"base_path": "/apps"}, {}, "/apps/pay/production"),
    ("zookeeper", {}, {"destination_template": "/x/{env}"}, "/x/production"),
    ("mongo", {}, {}, "configs"),
    ("mongo-primary", {"collection": "pay_cfg", "env_as_collection": True}, {}, "pay_cfg_production"),
    ("mongo", {}, {"destination_template": "{tenant}_{env}"}, "pay_production"),
    ("linux_server", {"file_paths": ["/etc/app/config.env"]}, {}, "/etc/app/config_production.env"),
    ("linux_server", {"file_paths": ["config.env"], "base_directory": "/etc/app"}, {}, "/etc/app/config_production.env"),
    ("linux_server", {"file_paths": ["/etc/app/config.env"], "env_as_path": True}, {}, "/etc/app/production/config.env"),
    ("linux_server", {"file_paths": ["/etc/app/config.env"]}, {"env_as_filename": False}, "/etc/app/config.env"),
    ("linux_server", {}, {"destination_template": "/etc/{tenant}/{env}.env"}, "/etc/pay/production.env"),
    ("s3-minio", {"bucket": "b", "prefix": "cfg/"}, {}, "b/cfg/production"),
    ("s3", {"bucket": "b"}, {"destination_template": "{tenant}/{env}"}, "b/pay/production"),
    ("configmap", {"namespace": "ns", "configmap_name": "app"}, {}, "ns/app-production"),
    ("configmap-checkout", {"namespace": "ns", "configmap_name": "app", "env_as_namespace": True}, {},
     "production/app"),
    ("helm", {"namespace": "ns", "release_name": "rel"}, {}, "ns/rel"),
])
def test_where_a_sync_writes(datasource, settings, rule, expected):
    from app.pipeline.sinks.settings import Settings, kind_of

    typed = Settings.of(kind_of(datasource), datasource, **settings)
    assert destinations.destination_of(datasource, rule, "pay", "production", typed) == expected


def test_where_a_sync_writes_is_read_from_the_datasource_block_of_config_cue(monkeypatch):
    from app.pipeline import datasource_settings

    bundle = {"datasources": [{"redis": {"tag": "cache", "key_prefix": "pay:cfg"}},
                              {"linux_server": {"tag": "web", "file_paths": ["/srv/pay/app.env"], "env_as_path": True}}]}
    monkeypatch.setattr(datasource_settings, "_bundle", lambda: bundle)
    assert destinations.destination_of("redis-cache", {}, "pay", "production") == "pay:cfg"
    assert destinations.destination_of("linux_server-web", {}, "pay", "production") == "/srv/pay/production/app.env"
    # a rule cannot move it: the rule's copy of a datasource field is not read
    assert destinations.destination_of("redis-cache", {"key_prefix": "evil:", "redis_key_prefix": "evil:"},
                                       "pay", "production") == "pay:cfg"


def test_a_destination_pattern_is_a_glob_that_crosses_slashes():
    assert destinations.matches("/etc/pay/app/x.env", ["/etc/pay/*"])
    assert destinations.matches("pay:production", ["pay:*"])
    assert not destinations.matches("/etc/evil/x.env", ["/etc/pay/*"])
    assert not destinations.matches("PAY:production", ["pay:*"])    # case counts


# ── what a tenant may write ───────────────────────────────────────────────────

DECLARED = [
    {"name": "pay", "destinations": [{"datasource": "linux_server", "paths": ["/etc/pay/*"]}]},
    {"name": "evil", "destinations": [{"datasource": "linux_server", "paths": ["/etc/evil/*"]}]},
    {"name": "plain"},
]


@pytest.fixture
def declared(monkeypatch):
    monkeypatch.setattr(tenants, "declared", lambda: [dict(t) for t in DECLARED])


def test_a_tenant_writes_what_its_destinations_cover(declared):
    tenants.may_write("pay", "linux_server", "/etc/pay/app.env")
    tenants.may_write("evil", "linux_server", "/etc/evil/app.env")


def test_a_tenant_cannot_write_the_place_of_another(declared):
    with pytest.raises(tenants.DestinationRefused) as refused:
        tenants.may_write("evil", "linux_server", "/etc/pay/app.env")
    message = str(refused.value)
    assert "/etc/pay/app.env" in message and "tenant evil" in message and "/etc/evil/*" in message


def test_a_tenant_with_no_destinations_on_a_restricted_datasource_writes_nothing(declared):
    with pytest.raises(tenants.DestinationRefused, match="it has none there"):
        tenants.may_write("plain", "linux_server", "/etc/anything")
    with pytest.raises(tenants.DestinationRefused):
        tenants.may_write("default", "linux_server", "/etc/anything")      # not even the implicit tenant


def test_a_datasource_nobody_restricted_is_open_to_every_tenant(declared):
    tenants.may_write("evil", "redis", "whatever:production")
    tenants.may_write("plain", "mongo", "configs")


def test_with_no_tenants_declared_everything_is_allowed(monkeypatch):
    monkeypatch.setattr(tenants, "declared", lambda: [])
    tenants.may_write("default", "linux_server", "/etc/anything")


# ── the sync refuses before it opens anything ─────────────────────────────────

def _ctx(rule):
    return PayloadContext(platform="github", repo_url="http://gitea:3000/acme/configs.git", ref="main",
                          branch="main", commit_sha="abc", tag=None, pr_number=None, rule_config=rule,
                          parsed_payload={})


def test_a_sync_to_another_tenants_place_is_refused_before_a_sink_is_created(declared):
    rule = {"datasource": "linux_server", "file_path": "/etc/pay/app.env", "env_as_filename": False}
    etl = ETLResult()
    etl.files.append(ETLFile(file_path="a.env", env="production", flat_data={"a": "1"}, root_key=""))
    create = MagicMock()
    with patch("app.pipeline.sinks.create_sink", create), patch("app.ui.ledger._redis") as redis:
        with pytest.raises(tenants.DestinationRefused, match="not one of tenant evil's"):
            stage_sync.run_stage_sync(_ctx(rule), etl, "linux_server", "evil", 1)
    create.assert_not_called()
    redis.assert_not_called()               # not even a lease was taken
