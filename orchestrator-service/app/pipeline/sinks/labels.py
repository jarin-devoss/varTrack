"""
app/pipeline/sinks/labels.py
─────────────────────────────
VarTrack "managed-by" labels — stamped on every object written by any sink.

VarTrack:
    app.kubernetes.io/managed-by:  vartrack
    vartrack.io/tenant:            <tenant_id>
    vartrack.io/datasource:        <datasource>
    vartrack.io/env:               <env>
    vartrack.io/source-repo:       <repo>
    vartrack.io/source-branch:     <branch>
    vartrack.io/source-commit:     <commit_sha[:12]>
    vartrack.io/sync-file:         <file_path>

Usage
─────
Every sink calls ``VarTrackLabels.build(...)`` and decides how to attach
the result to its storage format:

    labels = VarTrackLabels.build(ctx)

    # Kubernetes (ConfigMap / Helm)
    metadata.labels.update(labels.as_k8s_labels())

    # S3
    put_object(..., Tagging=labels.as_s3_tags())

    # MongoDB
    doc.update(labels.as_mongo_meta())

    # Redis
    client.hset(meta_key, mapping=labels.as_dict())

    # ZooKeeper
    zk.set(meta_znode, labels.as_json().encode())
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# The fixed "managed-by" key — identical across all storage backends
MANAGED_BY_KEY   = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "vartrack"

# VarTrack-namespaced label prefix
_PREFIX = "vartrack.io"


@dataclass(frozen=True)
class VarTrackLabels:
    """
    Immutable set of VarTrack identity labels for one sync operation.

    Construct via ``VarTrackLabels.build(...)`` rather than directly.
    """
    tenant_id:   str
    datasource:  str
    env:         str
    repo:        str
    branch:      str
    commit_sha:  str   # full SHA stored internally
    file_path:   str

    # ── Constructors ──────────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        *,
        tenant_id:  str,
        datasource: str,
        env:        str,
        repo:       str,
        branch:     str,
        commit_sha: str,
        file_path:  str,
    ) -> "VarTrackLabels":
        return cls(
            tenant_id=tenant_id,
            datasource=datasource,
            env=env,
            repo=repo,
            branch=branch,
            commit_sha=commit_sha,
            file_path=file_path,
        )

    # ── Representations ───────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, str]:
        """
        Flat dict of all labels — canonical form used by all as_* helpers.

        Keys use the ``vartrack.io/`` prefix so they are namespaced and
        never collide with application-owned keys.
        """
        return {
            MANAGED_BY_KEY:               MANAGED_BY_VALUE,
            f"{_PREFIX}/tenant":          self.tenant_id,
            f"{_PREFIX}/datasource":      self.datasource,
            f"{_PREFIX}/env":             self.env,
            f"{_PREFIX}/source-repo":     self.repo,
            f"{_PREFIX}/source-branch":   self.branch,
            f"{_PREFIX}/source-commit":   self.commit_sha[:12],
            f"{_PREFIX}/sync-file":       self.file_path,
        }

    def as_k8s_labels(self) -> dict[str, str]:
        """
        Kubernetes-safe labels.

        K8s label values must be ≤ 63 chars and match
        ``[a-zA-Z0-9][-a-zA-Z0-9_.]*``.  Repo URLs and long paths are
        truncated and sanitised.
        """
        raw = self.as_dict()
        return {k: _k8s_safe(v) for k, v in raw.items()}

    def as_s3_tags(self) -> str:
        """
        URL-encoded tag string for S3 ``Tagging`` parameter.

        S3 allows up to 10 tags; we use 8 so callers have room for 2 more.
        Tag keys / values are percent-encoded per AWS spec.
        """
        from urllib.parse import quote
        return "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}"
            for k, v in self.as_dict().items()
        )

    def as_mongo_meta(self) -> dict[str, str]:
        """
        MongoDB document fields — prefixed with ``_vt_`` to avoid collision
        with application data keys.
        """
        return {
            "_vt_managed_by":  MANAGED_BY_VALUE,
            "_vt_tenant":      self.tenant_id,
            "_vt_datasource":  self.datasource,
            "_vt_env":         self.env,
            "_vt_repo":        self.repo,
            "_vt_branch":      self.branch,
            "_vt_commit":      self.commit_sha[:12],
            "_vt_file":        self.file_path,
        }

    def as_redis_meta(self) -> dict[str, str]:
        """
        Flat dict for HSET on a ``<prefix>:__vartrack__`` meta hash key.
        Same as as_dict() — Redis has no label concept so we use a
        dedicated meta hash alongside the data hash.
        """
        return self.as_dict()

    def as_zk_meta(self) -> bytes:
        """JSON-encoded bytes for a ``__vartrack__`` meta znode."""
        return json.dumps(self.as_dict(), sort_keys=True).encode("utf-8")

    def as_vercel_meta(self) -> dict[str, str]:
        """
        Vercel Environment Variable metadata fields stored as specially-
        named env vars prefixed with ``VARTRACK_``.

        Vercel does not support arbitrary metadata on env vars, so we store
        the label set as a single JSON env var.
        """
        return {
            "VARTRACK_MANAGED_BY": MANAGED_BY_VALUE,
            "VARTRACK_META":       json.dumps(self.as_dict(), sort_keys=True),
        }

    def as_helm_annotations(self) -> dict[str, str]:
        """
        Helm chart annotations added to the release via ``--set``.
        Uses the same dict as as_k8s_labels() — annotations share the
        same key format in Kubernetes.
        """
        return self.as_k8s_labels()

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def __repr__(self) -> str:
        return (
            f"VarTrackLabels(tenant={self.tenant_id!r}, "
            f"datasource={self.datasource!r}, env={self.env!r}, "
            f"commit={self.commit_sha[:12]!r})"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _k8s_safe(value: str, max_len: int = 63) -> str:
    """
    Sanitise a string for use as a Kubernetes label value.

    Rules (from k8s docs):
      - Must be empty string OR match ``(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?``
      - Max 63 characters
    """
    import re
    # Replace illegal characters with hyphens
    sanitised = re.sub(r"[^A-Za-z0-9\-_.]", "-", value)
    # Must start and end with alphanumeric
    sanitised = sanitised.strip("-_.")
    # Truncate
    if len(sanitised) > max_len:
        sanitised = sanitised[:max_len].rstrip("-_.")
    return sanitised or "unknown"
