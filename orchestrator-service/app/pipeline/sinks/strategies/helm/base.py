"""
app/pipeline/sinks/strategies/helm/base.py
───────────────────────────────────────────
Abstract base for Helm write strategies.

  ValuesFileStrategy  — write flat_data to a YAML values file (no helm CLI)
  UpgradeStrategy     — run `helm upgrade --install` against a cluster
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class HelmWriteStrategy(ABC):

    @abstractmethod
    def apply(
        self,
        flat_data: dict[str, str],
        release_name: str,
        namespace: str,
        chart: str | None,
        kubeconfig: str | None,
        context: str | None,
        extra_flags: list[str],
        dry_run: bool,
    ) -> dict[str, Any]:
        """
        Apply flat_data to the Helm target.
        Returns dict with at least {"written": int, "pruned": int}.
        """
