"""CLI sync finds its rule by tenant, datasource and — when several platforms write to it — platform."""
import pytest

from app.pipeline import schema_utils
from app.schema_registry import manager as registry


@pytest.fixture
def mgr(monkeypatch, tmp_path):
    monkeypatch.setattr(registry.settings, "SCHEMA_CACHE_DIR", tmp_path)
    m = registry.SchemaRegistryManager()
    m._entries["default:schemas"] = registry._TenantEntry(
        tenant_id="default", repo_url="r", branch="main", local_path=tmp_path,
        rules=[
            {"platform": "github", "datasource": "mongo", "file_name": "app.yaml"},
            {"platform": "gitea", "datasource": "mongo", "file_name": "other.yaml"},
            {"platform": "github", "datasource": "redis"},
        ],
    )
    monkeypatch.setattr(registry, "_manager_instance", m)
    return m


def test_the_rule_for_the_datasource_is_found(mgr):
    assert schema_utils.rule_config_for_datasource("default", "redis") == {"platform": "github", "datasource": "redis"}


def test_with_rules_on_several_platforms_the_platform_picks_the_rule(mgr):
    assert schema_utils.rule_config_for_datasource("default", "mongo", "github")["file_name"] == "app.yaml"
    assert schema_utils.rule_config_for_datasource("default", "mongo", "gitea")["file_name"] == "other.yaml"
    assert schema_utils.rule_config_for_datasource("default", "mongo", "gitlab") is None


def test_with_rules_on_several_platforms_and_none_named_nothing_is_guessed(mgr):
    with pytest.raises(schema_utils.AmbiguousRule, match="github, gitea|gitea, github"):
        schema_utils.rule_config_for_datasource("default", "mongo")


def test_a_platform_is_not_needed_when_one_rule_writes_to_the_datasource(mgr):
    assert schema_utils.rule_config_for_datasource("default", "redis", "github") == {"platform": "github", "datasource": "redis"}
    assert schema_utils.rule_config_for_datasource("default", "redis", "gitea") is None


def test_an_unknown_datasource_or_tenant_has_no_rule(mgr):
    assert schema_utils.rule_config_for_datasource("default", "s3") is None
    assert schema_utils.rule_config_for_datasource("acme", "mongo") is None


def test_the_loaded_rules_are_used_when_the_schema_repo_cannot_be_refreshed(mgr, monkeypatch):
    def offline(*a, **kw):
        raise OSError("git unreachable")

    monkeypatch.setattr(mgr, "get_or_clone", offline)
    assert schema_utils.rule_config_for_datasource("default", "redis") is not None
