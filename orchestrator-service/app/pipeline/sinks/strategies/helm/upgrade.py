"""
app/pipeline/sinks/strategies/helm/upgrade.py
───────────────────────────────────────────────
UpgradeStrategy — invoke ``helm upgrade --install``.

Values injection modes (rule_config["helm_values_mode"]):
  "set"   (default) — one --set key=value per flat key
  "file"            — render to a temp YAML file, pass as --values

VarTrack labels injected as commonAnnotations via --set so they appear
on every K8s resource the chart creates (requires chart to honour
commonAnnotations — standard in most Bitnami / community charts).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from app.pipeline.sinks.strategies.helm.base import HelmWriteStrategy
from app.pipeline.sinks.strategies.helm.values_file import _unflatten, _to_yaml

logger = logging.getLogger(__name__)


class UpgradeStrategy(HelmWriteStrategy):

    def __init__(
        self,
        *,
        helm_binary: str = "",
        values_mode: str = "set",
        wait: bool = False,
        wait_timeout: str = "5m",
        atomic: bool = False,
        force: bool = False,
        cleanup_on_fail: bool = False,
        no_hooks: bool = False,
        max_history: int = 10,
    ) -> None:
        self._helm         = helm_binary or os.getenv("HELM_BINARY", "") or (shutil.which("helm") or "")
        self._values_mode  = values_mode.lower()
        self._wait         = wait
        self._wait_timeout = wait_timeout
        self._atomic       = atomic
        self._force        = force
        self._cleanup      = cleanup_on_fail
        self._no_hooks     = no_hooks
        self._max_history  = max_history

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
        if not chart:
            raise ValueError("UpgradeStrategy requires rule_config['helm_chart']")
        if not self._helm:
            raise RuntimeError(
                "helm binary not found — install helm or set HELM_BINARY / "
                "rule_config['helm_binary']"
            )

        # Separate VarTrack meta from real values
        vt_keys   = {k: v for k, v in flat_data.items() if k.startswith("_vartrack.")}
        data_keys = {k: v for k, v in flat_data.items() if not k.startswith("_vartrack.")}

        cmd, tmp_paths = self._build_cmd(
            data_keys, vt_keys, release_name, namespace, chart,
            kubeconfig, context, extra_flags, dry_run,
        )

        logger.info("HelmUpgrade: %s", " ".join(cmd))

        subprocess_timeout = _helm_timeout_s(self._wait_timeout) + 30 if self._wait else 120
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=subprocess_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"helm upgrade timed out after {subprocess_timeout}s "
                f"(release={release_name}, chart={chart})"
            ) from exc
        finally:
            for p in tmp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass

        if result.returncode != 0:
            raise RuntimeError(
                f"helm upgrade failed (rc={result.returncode}):\n{result.stderr}"
            )

        return {
            "written":     len(data_keys),
            "pruned":      0,
            "helm_output": result.stdout,
        }

    def _build_cmd(
        self,
        data_keys: dict,
        vt_keys: dict,
        release_name: str,
        namespace: str,
        chart: str,
        kubeconfig: str | None,
        context: str | None,
        extra_flags: list[str],
        dry_run: bool,
    ) -> tuple[list[str], list[str]]:
        """Return (cmd, tmp_paths) where tmp_paths are temp files to clean up."""
        cmd = [
            self._helm, "upgrade", "--install",
            release_name, chart,
            "--namespace", namespace,
            "--create-namespace",
            "--history-max", str(self._max_history),
        ]
        if kubeconfig:
            cmd += ["--kubeconfig", kubeconfig]
        if context:
            cmd += ["--kube-context", context]
        if self._wait:
            cmd += ["--wait", "--timeout", self._wait_timeout]
        if self._atomic:
            cmd.append("--atomic")
        if self._force:
            cmd.append("--force")
        if self._cleanup:
            cmd.append("--cleanup-on-fail")
        if self._no_hooks:
            cmd.append("--no-hooks")
        if dry_run:
            cmd.append("--dry-run")

        # Inject data values
        tmp_paths: list[str] = []
        if self._values_mode == "file":
            flags, tmp = _values_file_flags(data_keys)
            cmd += flags
            tmp_paths.append(tmp)
        else:
            cmd += _set_flags(data_keys)

        # Inject VarTrack labels as commonAnnotations
        for k, v in vt_keys.items():
            label_key = k[len("_vartrack."):]
            # Escape dots in key for --set
            escaped = label_key.replace(".", "\\.")
            cmd += ["--set-string", f"commonAnnotations.{escaped}={v}"]

        cmd += extra_flags
        return cmd, tmp_paths


def _helm_timeout_s(timeout_str: str) -> int:
    """Parse a helm duration string (e.g. '5m', '30s', '1h') to seconds."""
    import re
    m = re.fullmatch(r'(\d+)(s|m|h)', timeout_str.strip())
    if not m:
        return 300  # 5 minute default
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def _set_flags(flat_data: dict[str, str]) -> list[str]:
    flags = []
    for k, v in flat_data.items():
        v_esc = v.replace("\\", "\\\\").replace(",", "\\,")
        flags += ["--set", f"{k}={v_esc}"]
    return flags


def _values_file_flags(flat_data: dict[str, str]) -> tuple[list[str], str]:
    """Return (["--values", path], path) — caller is responsible for cleanup."""
    content = _to_yaml(_unflatten(flat_data))
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="vartrack_helm_",
        delete=False, encoding="utf-8",
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return ["--values", tmp.name], tmp.name
