"""
app/pipeline/sinks/strategies/mongo/file.py
────────────────────────────────────────────
FileStrategy — one MongoDB document per synced file (STRATEGY_FILE), "data"
holding the file in the shape it parsed to: nested for JSON, YAML, TOML and
the other tree formats, flat for KEY=VALUE files (.env, .properties).

Document shape (a .properties file; a YAML file's "data" is nested, as in
DocumentStrategy)
──────────────
{
    "_vt_tenant":     "acme",
    "_vt_datasource": "payments",
    "_vt_env":        "production",
    "_vt_managed_by": "vartrack",
    "_vt_repo":       "github.com/acme/config",
    "_vt_branch":     "main",
    "_vt_commit":     "a1b2c3d4e5f6",
    "_vt_file":       "services/payments/production.properties",
    "data": {
        "database.host": "prod-db.internal",
        "database.port": "5432",
        "cache.ttl":     "300",
    }
}

Index recommendation
────────────────────
db.configs.createIndex(
    { "_vt_tenant": 1, "_vt_datasource": 1, "_vt_env": 1, "_vt_file": 1 },
    { unique: true }
)
"""
from __future__ import annotations

import logging
from typing import Any

from app.pipeline.sinks.strategies.mongo.base import MongoWriteStrategy
from app.pipeline.transformer import _flatten

logger = logging.getLogger(__name__)


class FileStrategy(MongoWriteStrategy):

    def upsert(self, collection, flat_data, doc_filter, commit_sha, file_path, meta, data=None) -> int:
        flt = {**doc_filter, "_vt_file": file_path}
        doc = {**meta, **doc_filter, "_vt_file": file_path, "data": self._shape(flat_data, data)}
        collection.replace_one({**flt, "data": {"$exists": True}}, doc, upsert=True)
        # One document per key for this file, left from before the
        # datasource's update_strategy changed, no longer says anything.
        collection.delete_many({**flt, "data": {"$exists": False}})
        return len(flat_data)

    def repair(self, collection, flat_data, doc_filter, commit_sha, file_path, meta, data=None) -> int:
        flt      = {**doc_filter, "_vt_file": file_path}
        existing = collection.find_one(flt, {"_vt_commit": 1})
        if existing is None or existing.get("_vt_commit") != commit_sha:
            return self.upsert(collection, flat_data, doc_filter, commit_sha, file_path, meta, data=data)
        return 0

    def prune(self, collection, flat_data, doc_filter, protection, last) -> int:
        # File strategy replaces the whole doc on every write — stale keys
        # never persist, so prune is a no-op.
        return 0

    def read_values(self, collection, keys, doc_filter) -> dict[str, str]:
        # Flat or nested, "data" reads back as the ETL's flat keys.
        wanted = set(keys)
        out: dict[str, str] = {}
        for doc in collection.find({**doc_filter, "data": {"$exists": True}}, {"data": 1, "_id": 0}):
            for key, value in _flatten(doc.get("data") or {}).items():
                if key in wanted:
                    out[key] = value
        return out

    @staticmethod
    def _shape(flat_data: dict, data: Any) -> Any:
        """What "data" holds: the file as it parsed (the flat keys when that is not known)."""
        return data if data is not None else flat_data
