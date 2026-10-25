"""Helm write modes: what each upgrade_strategy runs."""
from __future__ import annotations

import pytest

from app.pipeline.sinks.strategies.helm import UpgradeStrategy, ValuesFileStrategy, get_strategy


def command(name: str, **rule) -> list[str]:
    strategy = get_strategy(name, {"helm_binary": "helm", **rule})
    cmd, _ = strategy._build_cmd({"replicaCount": "3"}, {}, "app", "shop", "./chart", None, None, [], False)
    return cmd


@pytest.mark.parametrize("name", ["upgrade", "install_or_upgrade", "INSTALL_OR_UPGRADE"])
def test_install_or_upgrade(name):
    cmd = command(name)
    assert cmd[:3] == ["helm", "upgrade", "--install"]
    assert "--create-namespace" in cmd and "--reuse-values" not in cmd


def test_full_upgrade_needs_the_release():
    cmd = command("FULL_UPGRADE")
    assert "--install" not in cmd and "--create-namespace" not in cmd and "--reuse-values" not in cmd


def test_values_only_keeps_the_other_values():
    cmd = command("VALUES_ONLY")
    assert "--install" not in cmd and "--reuse-values" in cmd
    assert "replicaCount=3" in " ".join(cmd)


def test_chart_version_and_repo():
    cmd = command("upgrade", helm_chart_version="1.4.2", helm_repo="https://charts.example.com")
    assert cmd[cmd.index("--version") + 1] == "1.4.2"
    assert cmd[cmd.index("--repo") + 1] == "https://charts.example.com"


def test_values_file_stays_available():
    assert isinstance(get_strategy("values_file", {}), ValuesFileStrategy)
    assert isinstance(get_strategy("upgrade", {}), UpgradeStrategy)
    with pytest.raises(ValueError, match="Available"):
        get_strategy("rollback", {})


def test_config_cue_sets_the_strategy():
    from app.pipeline.datasource_settings import sink_settings
    s = sink_settings("helm", {"upgrade_strategy": "FULL_UPGRADE", "chart_version": "2.0.0"}, "helm", {})
    assert s == {"helm_upgrade_strategy": "full_upgrade", "helm_chart_version": "2.0.0"}
    assert sink_settings("helm", {"upgrade_strategy": "UPGRADE_STRATEGY_UNSPECIFIED"}, "helm", {}) == {}
