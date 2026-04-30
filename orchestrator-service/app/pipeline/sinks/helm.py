"""
app/pipeline/sinks/helm.py
───────────────────────────
Helm sink — registered as "helm" by IFactory.

VarTrack labels are passed as Helm release annotations so every
managed release is stamped identically to other tools:

    helm upgrade --install payments ./chart \
      --set-string "commonAnnotations.app\\.kubernetes\\.io/managed-by=vartrack" \
      --set-string "commonAnnotations.vartrack\\.io/tenant=acme" \
      ...

For values_file strategy the labels are written as a top-level
``_vartrack`` block in the values YAML:

    _vartrack:
      app.kubernetes.io/managed-by: vartrack
      vartrack.io/tenant: acme
      ...
"""
from __future__ import annotations

import logging
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import resolve_destination
from app.pipeline.sinks.strategies.helm import get_strategy, HelmWriteStrategy
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)


class HelmSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "helm",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        cfg = rule_config or {}
        self._tenant_id      = tenant_id
        self._release_name   = cfg.get("helm_release_name", "vartrack")
        # destination_template: release name template, e.g. "app-{env}".
        # Takes precedence over helm_release_name when set.
        self._dest_tpl = cfg.get("destination_template", "")
        self._namespace    = cfg.get("helm_namespace", "default")
        self._chart        = cfg.get("helm_chart")
        self._kubeconfig   = cfg.get("helm_kubeconfig_path")
        self._context      = cfg.get("helm_context")
        self._extra_flags  = cfg.get("helm_extra_flags", [])

        strategy_name: str = cfg.get("helm_upgrade_strategy", "values_file")
        self._strategy: HelmWriteStrategy = get_strategy(strategy_name, cfg)

    def _write(
        self,
        *,
        flat_data: dict[str, str],
        sync_mode: SyncMode,
        datasource: str,
        env: str,
        repo: str,
        branch: str,
        commit_sha: str,
        file_path: str,
        labels: VarTrackLabels,
        prune: bool,
        prune_last: bool,
        prune_protection: list[str],
        dry_run_prune: bool,
        total_sources: int,
    ) -> dict[str, Any]:
        # Inject VarTrack annotations into the flat_data under a reserved prefix
        # so strategies can attach them appropriately (--set or values block)
        annotated_data = {**flat_data}
        for k, v in labels.as_helm_annotations().items():
            annotated_data[f"_vartrack.{k}"] = v

        release_name = (
            resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=env)
            if self._dest_tpl
            else self._release_name
        )
        return self._strategy.apply(
            flat_data=annotated_data,
            release_name=release_name,
            namespace=self._namespace,
            chart=self._chart,
            kubeconfig=self._kubeconfig,
            context=self._context,
            extra_flags=self._extra_flags,
            dry_run=False,
        )

    def close(self) -> None:
        pass
