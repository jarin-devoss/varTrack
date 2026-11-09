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

import json
import logging
from typing import Any

from app.pipeline.sinks.strategies.mongo.base import MongoWriteStrategy, Written, deleted
from app.pipeline.transformer import _flatten

logger = logging.getLogger(__name__)


def _in(session: Any) -> dict[str, Any]:
    """The keyword that puts an operation in *session*'s transaction (nothing when there is none)."""
    return {"session": session} if session is not None else {}


def _canonical(value: Any) -> str:
    """A document's data as text that tells True from 1 and keeps the order of nothing: what two copies of
    the same file compare by."""
    return json.dumps(value, sort_keys=True, default=str)


class FileStrategy(MongoWriteStrategy):
    # The document is written and the other-shape ones are removed: two operations, one transaction
    # where the server has them (MongoSink).
    transactional = True

    def upsert(self, collection, flat_data, doc_filter, commit_sha, file_path, meta, data=None,
               session=None) -> Written:
        flt = {**doc_filter, "_vt_file": file_path}
        doc = {**meta, **doc_filter, "_vt_file": file_path, "data": self._shape(flat_data, data)}
        opts = _in(session)
        collection.replace_one({**flt, "data": {"$exists": True}}, doc, upsert=True, **opts)
        # One document per key for this file, left from before the
        # datasource's update_strategy changed, no longer says anything. They
        # are counted: the sink logs it and the UI shows a Shape changed event.
        replaced = deleted(collection.delete_many({**flt, "data": {"$exists": False}}, **opts))
        return Written(len(flat_data), replaced)

    def repair(self, collection, flat_data, doc_filter, commit_sha, file_path, meta, data=None,
               session=None) -> Written:
        # The document is compared with what would be written, by content. The commit it was stamped with says
        # who wrote it, not what it holds now: a document edited in MongoDB keeps its stamp, and a heal runs at
        # the commit that is already there.
        flt      = {**doc_filter, "_vt_file": file_path}
        existing = collection.find_one({**flt, "data": {"$exists": True}}, {"data": 1}, **_in(session))
        wanted   = self._shape(flat_data, data)
        if existing is None or _canonical(existing.get("data")) != _canonical(wanted):
            return self.upsert(collection, flat_data, doc_filter, commit_sha, file_path, meta, data=data,
                               session=session)
        # It holds the file. Documents of the other shape for it are still taken away.
        replaced = deleted(collection.delete_many({**flt, "data": {"$exists": False}}, **_in(session)))
        return Written(0, replaced, len(flat_data))

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
