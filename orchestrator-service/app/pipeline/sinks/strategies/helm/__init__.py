"""
app/pipeline/sinks/strategies/helm/__init__.py
───────────────────────────────────────────────
Strategy factory for Helm write modes.

  "values_file"        — write YAML values file, no helm CLI  (default)
  "upgrade"            — helm upgrade --install
  "install_or_upgrade" — alias for upgrade
  "full_upgrade"       — alias for upgrade
  "values_only"        — alias for values_file  (matches proto enum name)
"""
from __future__ import annotations

from app.pipeline.sinks.strategies.helm.base import HelmWriteStrategy
from app.pipeline.sinks.strategies.helm.values_file import ValuesFileStrategy
from app.pipeline.sinks.strategies.helm.upgrade import UpgradeStrategy

__all__ = ["HelmWriteStrategy", "ValuesFileStrategy", "UpgradeStrategy", "get_strategy"]

_ALIASES: dict[str, str] = {
    "values_only":        "values_file",
    "full_upgrade":       "upgrade",
    "install_or_upgrade": "upgrade",
    "install":            "upgrade",
}


def get_strategy(name: str, rule_config: dict) -> HelmWriteStrategy:
    key = _ALIASES.get(name.lower(), name.lower())

    if key == "values_file":
        return ValuesFileStrategy(
            values_file_path=rule_config.get("helm_values_file_path", "values.yaml"),
            values_prefix=rule_config.get("helm_values_prefix", ""),
        )

    if key == "upgrade":
        return UpgradeStrategy(
            helm_binary=rule_config.get("helm_binary", ""),
            values_mode=rule_config.get("helm_values_mode", "set"),
            wait=bool(rule_config.get("helm_wait", False)),
            wait_timeout=rule_config.get("helm_wait_timeout", "5m"),
            atomic=bool(rule_config.get("helm_atomic", False)),
            force=bool(rule_config.get("helm_force", False)),
            cleanup_on_fail=bool(rule_config.get("helm_cleanup_on_fail", False)),
            no_hooks=bool(rule_config.get("helm_no_hooks", False)),
            max_history=int(rule_config.get("helm_max_history", 10)),
        )

    raise ValueError(
        f"Unknown helm_upgrade_strategy '{name}'. "
        f"Available: values_file, upgrade (aliases: values_only, full_upgrade, install_or_upgrade)"
    )
