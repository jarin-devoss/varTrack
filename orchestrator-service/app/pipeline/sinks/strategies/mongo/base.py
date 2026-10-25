"""
app/pipeline/sinks/strategies/mongo/base.py
────────────────────────────────────────────
Abstract base for MongoDB write strategies.

One concrete strategy per Strategy value (MongoConfig.update_strategy):
  KeyValueStrategy  — STRATEGY_KEY_VALUE: one MongoDB document per flat key
  DocumentStrategy  — STRATEGY_DOCUMENT:  one document per file, always nested
  FileStrategy      — STRATEGY_FILE:      one document per file, as it parsed
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MongoWriteStrategy(ABC):
    """
    Defines how flat_data is written to MongoDB.

    KeyValueStrategy  — one doc per flat key.
                        Shape: { _vt_tenant, _vt_datasource, _vt_env,
                                  key, value, _vt_commit, _vt_file, ... }
                        Fast individual key lookups.
                        Prune deletes stale key docs explicitly.

    FileStrategy      — one doc holds the whole file in the shape it parsed
                        to: nested for JSON, YAML, TOML …, flat for .env /
                        .properties.
                        Shape: { _vt_tenant, _vt_datasource, _vt_env,
                                  _vt_file, _vt_commit, data: {...} }
                        Atomic full-config replace.
                        Prune is a no-op — stale keys vanish on next write.

    DocumentStrategy  — as FileStrategy, always nested:
                        data: {database: {host: ...}}.
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
        data: Any = None,
    ) -> int:
        """Write flat_data (*data*: the file in the shape it parsed to). Returns number of docs written."""

    @abstractmethod
    def repair(
        self,
        collection,
        flat_data: dict[str, str],
        doc_filter: dict,
        commit_sha: str,
        file_path: str,
        meta: dict,
        data: Any = None,
    ) -> int:
        """Re-write docs that are missing or out of sync. Returns count repaired."""

    def read_values(self, collection, keys: list[str], doc_filter: dict) -> dict[str, str]:
        """Current values of *keys* (the ETL's flat keys), from one document per key."""
        docs = collection.find({**doc_filter, "key": {"$in": keys}}, {"key": 1, "value": 1, "_id": 0})
        return {doc["key"]: str(doc.get("value", "")) for doc in docs if "key" in doc}
