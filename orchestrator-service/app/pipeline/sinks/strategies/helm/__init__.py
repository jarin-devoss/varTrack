"""
app/pipeline/sinks/strategies/helm/__init__.py
───────────────────────────────────────────────
Strategy factory for Helm write modes.

  "values_file"        — write YAML values file, no helm CLI  (default)
  "upgrade"            — helm upgrade --install
  "install_or_upgrade" — alias for upgrade  (proto INSTALL_OR_UPGRADE)
  "full_upgrade"       — helm upgrade, no --install: the release must exist
  "values_only"        — helm upgrade --reuse-values, no --install: set our
                         values on the existing release, keep the rest
"""
from __future__ import annotations

from app.pipeline.sinks.strategies.helm.base import HelmWriteStrategy
from app.pipeline.sinks.strategies.helm.values_file import ValuesFileStrategy
from app.pipeline.sinks.strategies.helm.upgrade import UpgradeStrategy

__all__ = ["HelmWriteStrategy", "ValuesFileStrategy", "UpgradeStrategy", "get_strategy"]

_ALIASES: dict[str, str] = {
    "install_or_upgrade": "upgrade",
    "install":            "upgrade",
}

# Upgrade modes: (install a missing release, keep the release's other values)
_UPGRADE_MODES: dict[str, tuple[bool, bool]] = {
    "upgrade":      (True, False),
    "full_upgrade": (False, False),
    "values_only":  (False, True),
}


def get_strategy(name: str, rule_config: dict) -> HelmWriteStrategy:
    key = _ALIASES.get(name.lower(), name.lower())

    if key == "values_file":
        return ValuesFileStrategy(
            values_file_path=rule_config.get("helm_values_file_path", "values.yaml"),
            values_prefix=rule_config.get("helm_values_prefix", ""),
        )

    if key in _UPGRADE_MODES:
        install, reuse_values = _UPGRADE_MODES[key]
        return UpgradeStrategy(
            install=install,
            reuse_values=reuse_values,
            chart_version=str(rule_config.get("helm_chart_version") or ""),
            chart_repo=str(rule_config.get("helm_repo") or ""),
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
        f"Available: values_file, upgrade (or install_or_upgrade), full_upgrade, values_only"
    )
