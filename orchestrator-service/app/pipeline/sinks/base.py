"""
app/pipeline/sinks/base.py
───────────────────────────
Abstract base for all sink implementations.

VarTrackLabels are built here in write() before delegating to _write()
so every concrete sink automatically receives the labels without having
to construct them itself.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import Any

from app.utils.factory import IFactory
from app.pipeline.sinks.labels import VarTrackLabels


class BaseSink(IFactory, ABC):

    @classmethod
    def load_module(cls, name: str) -> None:
        import app.pipeline.sinks as sinks_pkg
        from app.utils.class_loader import load_class_from_package_module
        load_class_from_package_module(name, sinks_pkg)

    # ── Public entry point ────────────────────────────────────────────────────

    def write(
        self,
        *,
        flat_data: dict[str, str],
        sync_mode: Any,
        datasource: str,
        env: str,
        repo: str,
        branch: str,
        commit_sha: str,
        file_path: str,
        tenant_id: str = "default",
        prune: bool = False,
        prune_last: bool = False,
        prune_protection: list[str] | None = None,
        dry_run_prune: bool = False,
        total_sources: int = 1,
    ) -> dict[str, Any]:
        """
        Build VarTrack labels then delegate to ``_write()``.

        Labels are built once here so every concrete sink receives them
        without duplicating the construction logic.
        """
        labels = VarTrackLabels.build(
            tenant_id=tenant_id,
            datasource=datasource,
            env=env,
            repo=repo,
            branch=branch,
            commit_sha=commit_sha,
            file_path=file_path,
        )
        return self._write(
            flat_data=flat_data,
            sync_mode=sync_mode,
            datasource=datasource,
            env=env,
            repo=repo,
            branch=branch,
            commit_sha=commit_sha,
            file_path=file_path,
            labels=labels,
            prune=prune,
            prune_last=prune_last,
            prune_protection=prune_protection or [],
            dry_run_prune=dry_run_prune,
            total_sources=total_sources,
        )

    # ── Interface every sink must implement ───────────────────────────────────

    @abstractmethod
    def _write(
        self,
        *,
        flat_data: dict[str, str],
        sync_mode: Any,
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
        """Perform the actual write. Returns ``{"written": int, "pruned": int}``."""

    @abstractmethod
    def close(self) -> None:
        """Release any held connections / file handles."""

    def delete_file_data(self, datasource: str, env: str, file_path: str) -> int:
        """
        Delete previously written data for a specific file + env, used for
        transactional rollback when a later file in the same batch fails.

        Override in concrete sinks that support deletion (e.g. MongoSink).
        Default implementation is a no-op that logs a warning — rollback is
        best-effort for sinks that do not implement this method.

        Returns
        -------
        int
            Number of records deleted (0 for the default no-op).
        """
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "%s does not implement delete_file_data — "
            "rollback skipped for datasource=%s env=%s file=%s",
            type(self).__name__, datasource, env, file_path,
        )
        return 0

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """
        Read the current values of specific keys from the sink.

        Called by stage_sync before a write to capture the pre-write state
        for @logger change-log comparison.

        Override in concrete sinks that support point reads.
        Default implementation returns an empty dict — the change-log will
        emit "to=<new>" without a "from=<old>" component for those sinks.

        Parameters
        ----------
        keys:
            Flat field names to read (as they appear in flat_data).
        datasource:
            Datasource name — used to build the correct filter/key prefix.
        env:
            Environment string (e.g. "production", "pr-42").

        Returns
        -------
        dict mapping field_name → current_value for keys that exist.
        """
        return {}
