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
from collections.abc import Collection
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import resolve_destination
from app.pipeline.sinks.strategies.helm import get_strategy, HelmWriteStrategy
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)


class HelmSink(BaseSink):
    object_name = "release"

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
        total_sources: int,
        data: Any = None,
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

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """
        Read the values currently set on the release (or values file) for *env*.
        Never raises: stage_sync calls this before every write for the change-log.
        """
        if not keys:
            return {}
        from app.pipeline.sinks.strategies.helm.values_file import ValuesFileStrategy

        try:
            if isinstance(self._strategy, ValuesFileStrategy):
                import yaml
                with open(self._strategy._path, encoding="utf-8") as fh:
                    values = yaml.safe_load(fh) or {}
                values.pop("_vartrack", None)
                # The file holds keys without the strategy's values_prefix.
                flat = {self._strategy._prefix + k: v for k, v in _flatten(values).items()}
            else:
                flat = _flatten(self._release_values(env))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            return self._read_failed(exc, "HelmSink.read_values")
        return {k: flat[k] for k in keys if k in flat}

    def _release_values(self, env: str) -> dict:
        import json
        import os
        import shutil
        import subprocess

        release = (
            resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=env)
            if self._dest_tpl
            else self._release_name
        )
        helm = os.getenv("HELM_BINARY", "") or shutil.which("helm") or "helm"
        cmd = [helm, "get", "values", release, "--namespace", self._namespace, "--output", "json"]
        if self._kubeconfig:
            cmd += ["--kubeconfig", self._kubeconfig]
        if self._context:
            cmd += ["--kube-context", self._context]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            if "not found" in result.stderr:
                return {}
            raise RuntimeError(result.stderr.strip() or f"helm get values exited {result.returncode}")
        return json.loads(result.stdout or "null") or {}


def _scalar(value: Any) -> str:
    # --set types "true"/"false"/"null" (and integers): render them back the way they were written.
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    """{"a": {"b": "c"}} → {"a.b": "c"}"""
    if not isinstance(node, dict):
        return {prefix: _scalar(node)} if prefix else {}
    out: dict[str, str] = {}
    for k, v in node.items():
        out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    return out
