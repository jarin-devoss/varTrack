"""
app/pipeline/sinks/vercel.py
─────────────────────────────
Vercel sink — registered as "vercel" by IFactory.

VarTrack labels are stored as two special environment variables
(Vercel has no native label/annotation API for env vars):

    VARTRACK_MANAGED_BY = "vartrack"
    VARTRACK_META       = '{"app.kubernetes.io/managed-by": "vartrack", ...}'

These are always written alongside the data env vars so the Vercel
project is identifiably managed by VarTrack.
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_API_BASE = "https://api.vercel.com"


class VercelSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "vercel",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            import httpx as _httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required: pip install httpx") from exc

        cfg = rule_config or {}
        self._token      = cfg["vercel_token"]
        self._project_id = cfg["vercel_project_id"]
        self._team_id    = cfg.get("vercel_team_id")
        self._targets    = cfg.get("vercel_targets")
        self._sensitive  = cfg.get("vercel_sensitive_keys", [])
        self._api_base   = cfg.get("vercel_api_url", _API_BASE).rstrip("/")
        self._tenant_id  = tenant_id

        self._http = _httpx.Client(
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30,
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
        targets  = self._targets or self._default_targets(env)
        existing = self._fetch_existing()

        # Merge data + VarTrack meta env vars
        all_vars = {**flat_data, **labels.as_vercel_meta()}

        written = 0
        try:
            from vartrack_core import py_diff  # type: ignore[import]
            old_vals = {k: v.get("value", "") for k, v in existing.items()}
            diff = py_diff(old_vals, all_vars)
            for key, value in diff["added"].items():
                var_type = "sensitive" if self._is_sensitive(key) else "plain"
                self._create_env_var(key, value, targets, var_type)
                written += 1
            # Update changed keys; always re-send sensitive keys (encrypted values are not comparable)
            to_update = {k: v["new"] for k, v in diff["changed"].items()}
            for key in all_vars:
                if key in existing and self._is_sensitive(key):
                    to_update[key] = all_vars[key]
            for key, value in to_update.items():
                var_type = "sensitive" if self._is_sensitive(key) else "plain"
                self._update_env_var(existing[key]["id"], key, value, targets, var_type)
                written += 1
        except ImportError:
            for key, value in all_vars.items():
                var_type = "sensitive" if self._is_sensitive(key) else "plain"
                if key in existing:
                    self._update_env_var(existing[key]["id"], key, value, targets, var_type)
                else:
                    self._create_env_var(key, value, targets, var_type)
                written += 1

        pruned = 0
        if prune and not dry_run_prune:
            # Never prune the VarTrack meta vars
            protected = list(prune_protection) + ["VARTRACK_MANAGED_BY", "VARTRACK_META"]
            pruned = self._prune(flat_data, existing, protected, targets, prune_last)

        return {"written": written, "pruned": pruned}

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass

    def _qs(self) -> dict[str, str]:
        return {"teamId": self._team_id} if self._team_id else {}

    def _project_url(self, path: str = "") -> str:
        return f"{self._api_base}/v9/projects/{self._project_id}/env{path}"

    def _fetch_existing(self) -> dict[str, dict]:
        resp = self._http.get(self._project_url(), params=self._qs())
        resp.raise_for_status()
        return {item["key"]: item for item in resp.json().get("envs", [])}

    def _create_env_var(self, key: str, value: str, targets: list[str], var_type: str) -> None:
        resp = self._http.post(
            self._project_url(), params=self._qs(),
            json={"key": key, "value": value, "target": targets, "type": var_type},
        )
        resp.raise_for_status()

    def _update_env_var(self, var_id: str, key: str, value: str, targets: list[str], var_type: str) -> None:
        resp = self._http.patch(
            self._project_url(f"/{var_id}"), params=self._qs(),
            json={"value": value, "target": targets, "type": var_type},
        )
        resp.raise_for_status()

    def _delete_env_var(self, var_id: str) -> None:
        resp = self._http.delete(self._project_url(f"/{var_id}"), params=self._qs())
        resp.raise_for_status()

    def _prune(
        self, flat_data: dict, existing: dict,
        protection: list[str], targets: list[str], last: bool,
    ) -> int:
        git_keys   = set(flat_data.keys())
        to_delete  = [
            (key, meta) for key, meta in existing.items()
            if key not in git_keys
            and not any(fnmatch.fnmatch(key, p) for p in protection)
        ]
        if last:
            to_delete = to_delete[-1:]
        for key, meta in to_delete:
            self._delete_env_var(meta["id"])
        return len(to_delete)

    def _is_sensitive(self, key: str) -> bool:
        return any(fnmatch.fnmatch(key, p) for p in self._sensitive)

    @staticmethod
    def _default_targets(env: str) -> list[str]:
        return ["production"] if env == "production" else ["preview"]
