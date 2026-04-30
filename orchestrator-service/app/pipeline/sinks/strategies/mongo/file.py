"""
app/pipeline/sinks/strategies/mongo/file.py
────────────────────────────────────────────
FileStrategy — one MongoDB document holds the entire flat_data dict.

Document shape
──────────────
{
    "_vt_tenant":     "acme",
    "_vt_datasource": "payments",
    "_vt_env":        "production",
    "_vt_managed_by": "vartrack",
    "_vt_repo":       "github.com/acme/config",
    "_vt_branch":     "main",
    "_vt_commit":     "a1b2c3d4e5f6",
    "_vt_file":       "services/payments/production.yaml",
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

from app.pipeline.sinks.strategies.mongo.base import MongoWriteStrategy

logger = logging.getLogger(__name__)


class FileStrategy(MongoWriteStrategy):

    def upsert(self, collection, flat_data, doc_filter, commit_sha, file_path, meta) -> int:
        flt = {**doc_filter, "_vt_file": file_path}
        doc = {**meta, **doc_filter, "_vt_file": file_path, "data": flat_data}
        collection.replace_one(flt, doc, upsert=True)
        return len(flat_data)

    def repair(self, collection, flat_data, doc_filter, commit_sha, file_path, meta) -> int:
        flt      = {**doc_filter, "_vt_file": file_path}
        existing = collection.find_one(flt, {"_vt_commit": 1})
        if existing is None or existing.get("_vt_commit") != commit_sha:
            return self.upsert(collection, flat_data, doc_filter, commit_sha, file_path, meta)
        return 0

    def prune(self, collection, flat_data, doc_filter, protection, last) -> int:
        # File strategy replaces the whole doc on every write — stale keys
        # never persist, so prune is a no-op.
        return 0
