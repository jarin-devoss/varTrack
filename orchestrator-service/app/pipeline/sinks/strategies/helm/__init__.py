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


def get_strategy(name: str, settings, rule_config: dict | None = None) -> HelmWriteStrategy:
    """The write strategy *name* for a helm datasource's *settings*; the rule says only how values are
    passed (helm_values_mode). Everything that reaches helm's argv or the disk is the datasource's."""
    key = _ALIASES.get(name.lower(), name.lower())
    rule = rule_config or {}

    if key == "values_file":
        return ValuesFileStrategy(
            values_file_path=settings.get("values_file_path") or "values.yaml",
            values_prefix=settings.get("values_prefix", ""),
        )

    if key in _UPGRADE_MODES:
        install, reuse_values = _UPGRADE_MODES[key]
        wait_timeout = settings.seconds("wait_timeout")
        return UpgradeStrategy(
            install=install,
            reuse_values=reuse_values,
            chart_version=str(settings.get("chart_version") or ""),
            chart_repo=str(settings.get("chart_repo") or ""),
            values_mode=rule.get("helm_values_mode", "set"),
            wait=bool(settings.get("wait", False)),
            wait_timeout=f"{wait_timeout}s" if wait_timeout else "5m",
            atomic=bool(settings.get("atomic", False)),
            force=bool(settings.get("force", False)),
            cleanup_on_fail=bool(settings.get("cleanup_on_fail", False)),
            no_hooks=bool(settings.get("no_hooks", False)),
            max_history=int(settings.get("max_history", 10)),
        )

    raise ValueError(
        f"Unknown helm_upgrade_strategy '{name}'. "
        f"Available: values_file, upgrade (or install_or_upgrade), full_upgrade, values_only"
    )
