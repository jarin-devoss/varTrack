"""
app/pipeline/sinks/s3.py
─────────────────────────
S3 sink — registered as "s3" by IFactory.

VarTrack labels are attached as S3 object tags on every uploaded object:

    app.kubernetes.io%2Fmanaged-by=vartrack
    vartrack.io%2Ftenant=acme
    vartrack.io%2Fdatasource=payments
    vartrack.io%2Fenv=production
    vartrack.io%2Fsource-repo=github.com%2Facme%2Fconfig
    vartrack.io%2Fsource-branch=main
    vartrack.io%2Fsource-commit=a1b2c3d4e5f6
    vartrack.io%2Fsync-file=services%2Fpayments%2Fproduction.yaml

Additional tags from rule_config["s3_object_tags"] and rule_config["s3_tag_keys"]
are merged in — VarTrack labels always win on key collision.
S3 hard limit: 10 tags total.
"""
from __future__ import annotations

import json
import logging
import mimetypes
from typing import Any
from urllib.parse import quote, unquote

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import resolve_destination
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_S3_TAG_LIMIT = 10


class S3Sink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "s3",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required: pip install boto3") from exc

        cfg = rule_config or {}
        self._bucket          = cfg["s3_bucket"]
        self._prefix          = cfg.get("s3_key_prefix", "").rstrip("/")
        self._one_per_key     = bool(cfg.get("s3_one_file_per_key", False))
        self._static_tags     = cfg.get("s3_object_tags", {})
        self._dynamic_tag_keys= cfg.get("s3_tag_keys", [])
        self._tenant_id       = tenant_id
        # destination_template: path prefix template, e.g. "{tenant}/{env}".
        # Takes precedence over s3_key_prefix when set.
        self._dest_tpl  = cfg.get("destination_template", "")

        self._s3 = boto3.client(
            "s3",
            region_name=cfg.get("s3_region"),
            aws_access_key_id=cfg.get("s3_access_key_id"),
            aws_secret_access_key=cfg.get("s3_secret_access_key"),
            endpoint_url=cfg.get("s3_endpoint_url"),
        )

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
        # Build final tag string: static → dynamic → vartrack labels (last wins)
        tagging = self._resolve_tags(flat_data, labels)

        if self._one_per_key:
            written = self._put_per_key(flat_data, datasource, env, tagging)
        else:
            written = self._put_single(flat_data, datasource, env, tagging)

        return {"written": written, "pruned": 0}

    def close(self) -> None:
        pass

    # ── Tag resolution ────────────────────────────────────────────────────────

    def _resolve_tags(self, flat_data: dict[str, str], labels: VarTrackLabels) -> str:
        """
        Merge tag sources (low → high priority):
          1. static tags from rule_config["s3_object_tags"]
          2. dynamic tags from rule_config["s3_tag_keys"] values in flat_data
          3. VarTrack managed-by labels  ← always win
        Result is URL-encoded and capped at S3's 10-tag limit.
        """
        merged: dict[str, str] = {}

        # 1. static
        merged.update(self._static_tags)

        # 2. dynamic — pull values from flat_data at runtime
        for key in self._dynamic_tag_keys:
            if key in flat_data:
                merged[key] = flat_data[key]

        # 3. VarTrack labels always override
        # Parse the labels tag string back to dict for merge
        vt_tags = dict(
            (unquote(k), unquote(v))
            for part in labels.as_s3_tags().split("&")
            for k, _, v in [part.partition("=")]
            if k
        )
        merged.update(vt_tags)

        # Cap at 10 — VarTrack labels survive truncation
        if len(merged) > _S3_TAG_LIMIT:
            vt_keys  = set(vt_tags.keys())
            non_vt   = [(k, v) for k, v in merged.items() if k not in vt_keys]
            keep     = dict(non_vt[:_S3_TAG_LIMIT - len(vt_keys)])
            keep.update(vt_tags)
            merged   = keep

        return "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}"
            for k, v in merged.items()
        )

    # ── Upload helpers ────────────────────────────────────────────────────────

    def _key_prefix(self, env: str) -> str:
        """Resolve the S3 key prefix for this env."""
        if self._dest_tpl:
            return resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=env).strip("/")
        return f"{self._prefix}/{env}".lstrip("/")

    def _put_single(
        self,
        flat_data: dict[str, str],
        datasource: str,
        env: str,
        tagging: str,
    ) -> int:
        key = f"{self._key_prefix(env)}/config.json"
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(flat_data, indent=2).encode(),
            ContentType="application/json",
            Tagging=tagging,
        )
        logger.debug("S3: put s3://%s/%s", self._bucket, key)
        return len(flat_data)

    def _put_per_key(
        self,
        flat_data: dict[str, str],
        datasource: str,
        env: str,
        tagging: str,
    ) -> int:
        for k, v in flat_data.items():
            key = f"{self._key_prefix(env)}/{k}"
            ct, _ = mimetypes.guess_type(k)
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=v.encode(),
                ContentType=ct or "application/octet-stream",
                Tagging=tagging,
            )
        return len(flat_data)
