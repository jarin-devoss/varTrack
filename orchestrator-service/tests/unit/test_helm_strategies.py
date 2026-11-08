"""Helm write modes: what each upgrade_strategy runs."""
from __future__ import annotations

import pytest

from app.pipeline.sinks.settings import Settings
from app.pipeline.sinks.strategies.helm import UpgradeStrategy, ValuesFileStrategy, get_strategy


@pytest.fixture(autouse=True)
def the_operators_helm(monkeypatch):
    # Which program runs is the operator's (HELM_BINARY), never a rule's.
    monkeypatch.setenv("HELM_BINARY", "helm")


def command(name: str, **settings) -> list[str]:
    strategy = get_strategy(name, Settings.of("helm", **settings))
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
    cmd = command("upgrade", chart_version="1.4.2", chart_repo="https://charts.example.com")
    assert cmd[cmd.index("--version") + 1] == "1.4.2"
    assert cmd[cmd.index("--repo") + 1] == "https://charts.example.com"


def test_values_file_stays_available():
    assert isinstance(get_strategy("values_file", Settings.of("helm")), ValuesFileStrategy)
    assert isinstance(get_strategy("upgrade", Settings.of("helm")), UpgradeStrategy)
    with pytest.raises(ValueError, match="Available"):
        get_strategy("rollback", Settings.of("helm"))


def test_config_cue_sets_the_strategy():
    from app.pipeline.sinks.helm import HelmSink

    sink = HelmSink(settings=Settings.of("helm", upgrade_strategy="FULL_UPGRADE", chart_version="2.0.0"))
    assert isinstance(sink._strategy, UpgradeStrategy)
    assert sink._strategy._install is False and sink._strategy._chart_version == "2.0.0"
    # unset (or *_UNSPECIFIED) is the values file, which runs no helm at all
    assert isinstance(HelmSink(settings=Settings.of("helm", upgrade_strategy="UPGRADE_STRATEGY_UNSPECIFIED"))._strategy,
                      ValuesFileStrategy)
