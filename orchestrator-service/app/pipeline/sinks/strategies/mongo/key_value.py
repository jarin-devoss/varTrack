"""
app/pipeline/sinks/strategies/mongo/key_value.py
──────────────────────────────────────────────────
KeyValueStrategy — one MongoDB document per flat key (STRATEGY_KEY_VALUE).

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
    "key":            "database.host",
    "value":          "prod-db.internal",
}

Index recommendation
────────────────────
db.configs.createIndex(
    { "_vt_tenant": 1, "_vt_datasource": 1, "_vt_env": 1, "key": 1 },
    { unique: true }
)
"""
from __future__ import annotations

import logging

from app.pipeline.sinks.strategies.mongo.base import MongoWriteStrategy, Written, deleted

logger = logging.getLogger(__name__)


class KeyValueStrategy(MongoWriteStrategy):

    @staticmethod
    def _drop_file_document(collection, doc_filter: dict, file_path: str) -> int:
        """The one document that held this file, left from before the datasource's
        update_strategy changed to key-value. Returns how many were removed."""
        return deleted(collection.delete_many({**doc_filter, "_vt_file": file_path, "data": {"$exists": True}}))

    def upsert(self, collection, flat_data, doc_filter, commit_sha, file_path, meta, data=None) -> Written:
        replaced = self._drop_file_document(collection, doc_filter, file_path)
        if not flat_data:
            return Written(0, replaced)
        import time
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError
        ops = [
            UpdateOne(
                {**doc_filter, "key": key},
                {"$set": {**meta, **doc_filter, "key": key, "value": value}},
                upsert=True,
            )
            for key, value in flat_data.items()
        ]
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                collection.bulk_write(ops, ordered=False)
                break
            except BulkWriteError as exc:
                if attempt >= max_retries:
                    raise
                logger.debug("bulk_write attempt %d failed: %s — retrying", attempt + 1, exc)
                time.sleep(0.1 * (attempt + 1))
        return Written(len(flat_data), replaced)

    def repair(self, collection, flat_data, doc_filter, commit_sha, file_path, meta, data=None) -> Written:
        replaced = self._drop_file_document(collection, doc_filter, file_path)
        try:
            from vartrack_core import py_diff  # type: ignore[import]
            _use_rust = True
        except ImportError:
            _use_rust = False

        if _use_rust:
            current = {
                doc["key"]: doc["value"]
                for doc in collection.find(doc_filter, {"key": 1, "value": 1, "_id": 0})
            }
            diff = py_diff(current, flat_data)
            if diff["is_empty"]:
                return Written(0, replaced)
            from pymongo import UpdateOne
            to_upsert = {
                **diff["added"],
                **{k: v["new"] for k, v in diff["changed"].items()},
            }
            ops = [
                UpdateOne(
                    {**doc_filter, "key": key},
                    {"$set": {**meta, **doc_filter, "key": key, "value": value}},
                    upsert=True,
                )
                for key, value in to_upsert.items()
            ]
            if ops:
                collection.bulk_write(ops, ordered=False)
            return Written(len(ops), replaced)

        # Fallback: per-key loop
        repaired = 0
        for key, value in flat_data.items():
            flt = {**doc_filter, "key": key}
            existing = collection.find_one(flt, {"value": 1, "_vt_commit": 1})
            if existing is None or existing.get("value") != value:
                doc = {**meta, **doc_filter, "key": key, "value": value}
                collection.update_one(flt, {"$set": doc}, upsert=True)
                repaired += 1
        return Written(repaired, replaced)
