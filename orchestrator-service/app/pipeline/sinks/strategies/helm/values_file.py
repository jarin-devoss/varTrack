"""
app/pipeline/sinks/strategies/helm/values_file.py
───────────────────────────────────────────────────
ValuesFileStrategy — unflattens flat_data and writes a Helm values YAML.

No helm CLI or cluster access required.  Downstream tooling picks the file
up and drives the actual release.

Dot-separated flat keys are unflattened to nested YAML:
    {"image.tag": "1.25", "replicaCount": "3"}
    →
    image:
      tag: "1.25"
    replicaCount: "3"

VarTrack labels (keys prefixed with ``_vartrack.``) are written as a
top-level ``_vartrack`` block so the values file is self-documenting:
    _vartrack:
      app.kubernetes.io/managed-by: vartrack
      vartrack.io/tenant: acme
      ...
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.pipeline.sinks.strategies.helm.base import HelmWriteStrategy

logger = logging.getLogger(__name__)


class ValuesFileStrategy(HelmWriteStrategy):

    def __init__(self, values_file_path: str, values_prefix: str = "") -> None:
        self._path   = values_file_path
        self._prefix = values_prefix.rstrip(".") + "." if values_prefix else ""

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
        # Separate VarTrack meta keys from real data
        vt_keys   = {k: v for k, v in flat_data.items() if k.startswith("_vartrack.")}
        data_keys = {k: v for k, v in flat_data.items() if not k.startswith("_vartrack.")}

        if self._prefix:
            data_keys = {
                k[len(self._prefix):]: v
                for k, v in data_keys.items()
                if k.startswith(self._prefix)
            }

        nested = _unflatten(data_keys)

        # Write _vartrack block with labels stripped of the leading prefix
        if vt_keys:
            nested["_vartrack"] = {
                k[len("_vartrack."):]: v for k, v in vt_keys.items()
            }

        content = _to_yaml(nested)

        if dry_run:
            logger.info("ValuesFileStrategy dry_run: would write %d keys to %s",
                        len(data_keys), self._path)
            return {"written": len(data_keys), "pruned": 0, "dry_run": True}

        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            fh.write(content)

        logger.info("ValuesFileStrategy: wrote %d keys to %s", len(data_keys), self._path)
        return {"written": len(data_keys), "pruned": 0}


def _unflatten(flat: dict[str, str], sep: str = ".") -> dict:
    result: dict = {}
    for key, value in flat.items():
        parts = key.split(sep)
        node  = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def _to_yaml(data: dict) -> str:
    try:
        import yaml
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)
    except ImportError:
        return "\n".join(f"{k}: {v}" for k, v in data.items()) + "\n"
