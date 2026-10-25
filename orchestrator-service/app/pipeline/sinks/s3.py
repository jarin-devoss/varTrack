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

The objects follow the datasource's update_strategy (S3Config):

  STRATEGY_KEY_VALUE — one object per key:   {prefix}/db.host
  STRATEGY_DOCUMENT  — {prefix}/config.json, always nested
  STRATEGY_FILE      — {prefix}/config.json, in the shape the file parsed to:
                       nested for JSON, YAML, TOML …, flat for .env / .properties
  unset              — as before: one object per key with s3_one_file_per_key,
                       otherwise {prefix}/config.json with the flat keys
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Collection
import mimetypes
from typing import Any
from urllib.parse import quote, unquote

from app.pipeline.sinks.base import BaseSink, update_strategy
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import resolve_destination
from app.pipeline.transformer import _flatten, as_nested
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_S3_TAG_LIMIT = 10


class S3Sink(BaseSink):
    object_name = "object"

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
        # S3Config.update_strategy; unset keeps s3_one_file_per_key.
        self._strategy        = update_strategy(cfg.get("update_strategy"))
        self._one_per_key     = self._strategy == "STRATEGY_KEY_VALUE" or (
            self._strategy == "STRATEGY_UNSPECIFIED" and bool(cfg.get("s3_one_file_per_key", False))
        )
        self._static_tags     = cfg.get("s3_object_tags", {})
        self._dynamic_tag_keys= cfg.get("s3_tag_keys", [])
        self._tenant_id       = tenant_id
        # destination_template: path prefix template, e.g. "{tenant}/{env}".
        # Takes precedence over s3_key_prefix when set.
        self._dest_tpl  = cfg.get("destination_template", "")

        # The single-document object's name; "{env}" is the environment.
        self._file_pattern    = cfg.get("s3_file_pattern") or "config.json"
        self._content_type    = cfg.get("s3_content_type") or ""
        # Server-side encryption on every object written: AES256 or aws:kms.
        self._put_extra: dict[str, str] = {}
        if cfg.get("s3_sse"):
            self._put_extra["ServerSideEncryption"] = cfg["s3_sse"]
        if cfg.get("s3_kms_key_id"):
            self._put_extra["SSEKMSKeyId"] = cfg["s3_kms_key_id"]

        client_options: dict[str, Any] = {}
        timeout = cfg.get("s3_timeout_secs")
        if cfg.get("s3_force_path_style") or timeout:
            from botocore.config import Config

            client_options["config"] = Config(
                s3={"addressing_style": "path"} if cfg.get("s3_force_path_style") else {},
                **({"connect_timeout": timeout, "read_timeout": timeout} if timeout else {}),
            )
        # verify_ssl false skips certificate checks; ssl_ca_cert names a CA bundle.
        if cfg.get("s3_verify_ssl") is False:
            client_options["verify"] = False
        elif cfg.get("s3_ssl_ca_cert"):
            client_options["verify"] = cfg["s3_ssl_ca_cert"]
        if cfg.get("s3_session_token"):
            client_options["aws_session_token"] = cfg["s3_session_token"]

        self._s3 = boto3.client(
            "s3",
            region_name=cfg.get("s3_region"),
            aws_access_key_id=cfg.get("s3_access_key_id"),
            aws_secret_access_key=cfg.get("s3_secret_access_key"),
            endpoint_url=cfg.get("s3_endpoint_url"),
            **client_options,
        )
        if cfg.get("s3_enable_versioning"):
            _enable_versioning(self._s3, self._bucket)

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
        # Build final tag string: static → dynamic → vartrack labels (last wins)
        tagging = self._resolve_tags(flat_data, labels)

        if self._one_per_key:
            written = self._put_per_key(flat_data, datasource, env, tagging)
        else:
            written = self._put_single(flat_data, self._document(flat_data, data), env, tagging)

        return {"written": written}

    def close(self) -> None:
        pass

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """Read current values back from the objects this sink writes for *env*."""
        if not keys:
            return {}
        prefix = self._key_prefix(env)
        try:
            if self._one_per_key:
                out: dict[str, str] = {}
                for k in keys:
                    try:
                        obj = self._s3.get_object(Bucket=self._bucket, Key=f"{prefix}/{k}")
                    except self._s3.exceptions.NoSuchKey:
                        continue
                    out[k] = obj["Body"].read().decode("utf-8")
                return out
            obj = self._s3.get_object(Bucket=self._bucket, Key=f"{prefix}/{self._object_name(env)}")
            # Flat or nested, config.json reads back as the ETL's flat keys.
            stored = _flatten(json.loads(obj["Body"].read()))
            return {k: stored[k] for k in keys if k in stored}
        except self._s3.exceptions.NoSuchKey:
            return {}
        except Exception as exc:
            return self._read_failed(exc, "S3Sink.read_values")

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

    def _document(self, flat_data: dict[str, str], data: Any) -> Any:
        """What config.json holds: always nested for DOCUMENT, the file as it parsed for FILE."""
        if self._strategy == "STRATEGY_DOCUMENT":
            return as_nested(data if data is not None else flat_data)
        if self._strategy == "STRATEGY_FILE" and data is not None:
            return data
        return flat_data

    def _object_name(self, env: str) -> str:
        return self._file_pattern.replace("{env}", env)

    def _put_single(
        self,
        flat_data: dict[str, str],
        document: Any,
        env: str,
        tagging: str,
    ) -> int:
        key = f"{self._key_prefix(env)}/{self._object_name(env)}"
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(document, indent=2).encode(),
            ContentType=self._content_type or "application/json",
            Tagging=tagging,
            **self._put_extra,
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
                ContentType=self._content_type or ct or "application/octet-stream",
                Tagging=tagging,
                **self._put_extra,
            )
        return len(flat_data)


_versioned: set[str] = set()
_versioned_lock = threading.Lock()


def _enable_versioning(s3: Any, bucket: str) -> None:
    """Turn on versioning for *bucket*, once per process. Without the
    permission to, the write goes on and the warning says why."""
    with _versioned_lock:
        if bucket in _versioned:
            return
        _versioned.add(bucket)
    try:
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        logger.info("S3: versioning enabled on bucket %s", bucket)
    except Exception as exc:
        logger.warning("S3: cannot enable versioning on bucket %s: %s", bucket, exc)
