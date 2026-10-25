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
from typing import Any, NamedTuple


class Written(NamedTuple):
    """What a write did: documents written, and documents of the other shape
    removed because the datasource's update_strategy changed since the file
    was last written (a key-value document per key vs one document per file)."""
    written:  int
    replaced: int = 0


def deleted(result: Any) -> int:
    """How many documents a delete_many removed."""
    count = getattr(result, "deleted_count", 0)
    return count if isinstance(count, int) else 0


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
    ) -> Written:
        """Write flat_data (*data*: the file in the shape it parsed to). Returns the
        number of docs written and of other-shape docs replaced."""

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
    ) -> Written:
        """Re-write docs that are missing or out of sync. Returns the count repaired
        and the other-shape docs replaced."""

    def read_values(self, collection, keys: list[str], doc_filter: dict) -> dict[str, str]:
        """Current values of *keys* (the ETL's flat keys), from one document per key."""
        docs = collection.find({**doc_filter, "key": {"$in": keys}}, {"key": 1, "value": 1, "_id": 0})
        return {doc["key"]: str(doc.get("value", "")) for doc in docs if "key" in doc}
