"""
app/pipeline/sinks/strategies/mongo/base.py
────────────────────────────────────────────
Abstract base for MongoDB write strategies.

Two concrete strategies:
  DocumentStrategy  — one MongoDB document per flat key
  FileStrategy      — one MongoDB document holds the entire flat_data dict
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class MongoWriteStrategy(ABC):
    """
    Defines how flat_data is written to MongoDB.

    DocumentStrategy  — one doc per flat key.
                        Shape: { _vt_tenant, _vt_datasource, _vt_env,
                                  key, value, _vt_commit, _vt_file, ... }
                        Fast individual key lookups.
                        Prune deletes stale key docs explicitly.

    FileStrategy      — one doc holds the entire flat_data dict.
                        Shape: { _vt_tenant, _vt_datasource, _vt_env,
                                  _vt_file, _vt_commit, data: {key: value} }
                        Atomic full-config replace.
                        Prune is a no-op — stale keys vanish on next write.
    """

    @abstractmethod
    def upsert(
        self,
        collection,
        flat_data: dict[str, str],
        doc_filter: dict,
        commit_sha: str,
        file_path: str,
        meta: dict,
    ) -> int:
        """Write flat_data. Returns number of docs written."""

    @abstractmethod
    def repair(
        self,
        collection,
        flat_data: dict[str, str],
        doc_filter: dict,
        commit_sha: str,
        file_path: str,
        meta: dict,
    ) -> int:
        """Re-write docs that are missing or out of sync. Returns count repaired."""

    @abstractmethod
    def prune(
        self,
        collection,
        flat_data: dict[str, str],
        doc_filter: dict,
        protection: list[str],
        last: bool,
    ) -> int:
        """Delete stale docs no longer in flat_data. Returns count pruned."""
