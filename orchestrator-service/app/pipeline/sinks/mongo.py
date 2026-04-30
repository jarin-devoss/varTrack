"""
app/pipeline/sinks/mongo.py
────────────────────────────
MongoDB sink.

Key format: DOT only
────────────────────
Mongo is a document store.  Dot-separated keys are natural JSON field names —
no path conversion is needed or allowed.

  Document strategy (default):
    {
      "_vt_tenant": "acme", "_vt_datasource": "payments", "_vt_env": "pr-42",
      "key":   "db.host",
      "value": "localhost"
    }

  File strategy:
    {
      "_vt_tenant": "acme", "_vt_datasource": "payments", "_vt_env": "pr-42",
      "data": {
        "db.host": "localhost",
        "db.port": "5432"
      }
    }

Auto-provision
──────────────
When an env is seen for the first time (e.g. "pr-42" from a new PR),
the sink auto-provisions a new collection:

  collection name = "{base_collection}_{safe_env}"
  e.g.  configs_pr-42   configs_v1.2.3   configs_feature_login

MongoDB creates the collection implicitly on the first upsert — no explicit
CREATE call is needed.

Set rule_config["env_as_collection"] = true to enable per-env collections.
When false (default), all envs share one collection and the env is only
stored as a _vt_env field.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Collection
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import resolve_destination
from app.pipeline.sinks.strategies.mongo import get_strategy, MongoWriteStrategy
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_DEFAULT_DB         = "vartrack"
_DEFAULT_COLLECTION = "configs"
_DEFAULT_STRATEGY   = "document"


class MongoSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "mongo",
        mongo_uri: str | None = None,
        database: str | None = None,
        collection: str | None = None,
        tenant_id: str = "default",
        rule_config: dict | None = None,
        **_kwargs: Any,
    ) -> None:
        try:
            import pymongo
        except ImportError as exc:
            raise RuntimeError("pymongo is required: pip install pymongo") from exc

        cfg = rule_config or {}
        uri = (
            mongo_uri
            or cfg.get("mongo_uri")
            or os.getenv("MONGO_URI", "mongodb://localhost:27017")
        )

        tls_opts: dict[str, object] = {}
        if cfg.get("mongo_tls") or os.getenv("MONGO_TLS", "").lower() in ("1", "true", "yes"):
            tls_opts["tls"] = True
        ca_file = cfg.get("mongo_tls_ca_file") or os.getenv("MONGO_TLS_CA_FILE", "")
        if ca_file:
            tls_opts["tlsCAFile"] = ca_file
            tls_opts.setdefault("tls", True)
        if cfg.get("mongo_tls_insecure") or os.getenv("MONGO_TLS_INSECURE", "").lower() in ("1", "true", "yes"):
            tls_opts["tlsAllowInvalidCertificates"] = True

        self._client           = pymongo.MongoClient(uri, **tls_opts)
        self._db               = self._client[database or cfg.get("database") or _DEFAULT_DB]
        self._base_collection  = collection or cfg.get("collection") or _DEFAULT_COLLECTION
        self._tenant_id        = tenant_id
        self._env_as_collection = bool(cfg.get("env_as_collection", False))
        # destination_template: collection name template, e.g. "{env}-config".
        # Takes precedence over env_as_collection logic when set.
        self._dest_tpl   = cfg.get("destination_template", "")

        strategy_name: str = cfg.get("mongo_strategy", _DEFAULT_STRATEGY)
        self._strategy: MongoWriteStrategy = get_strategy(strategy_name)

        # Mongo only supports DOT format — validate here so misconfiguration
        # is caught at construction, not at write time.
        if cfg.get("key_format") and cfg["key_format"] != "dot":
            raise ValueError(
                "MongoSink only supports key_format='dot'. "
                "Mongo is a document store — slash/flat paths are not valid here."
            )

        logger.debug(
            "MongoSink ready strategy=%s env_as_collection=%s",
            strategy_name, self._env_as_collection,
        )

        # Ensure the compound index exists on the filter fields.
        # MongoDB upsert/find without an index performs a full collection scan
        # — O(n) per write that degrades severely at scale.
        # background=True means the index build does not block ongoing reads.
        # create_index is idempotent: calling it when the index already exists
        # is a cheap no-op.
        try:
            import pymongo
            self._db[self._base_collection].create_index(
                [
                    ("_vt_tenant", pymongo.ASCENDING),
                    ("_vt_datasource", pymongo.ASCENDING),
                    ("_vt_env", pymongo.ASCENDING),
                ],
                name="vt_tenant_ds_env",
                background=True,
            )
            logger.debug("MongoSink: compound index ensured on %s", self._base_collection)
        except Exception:
            logger.warning(
                "MongoSink: failed to create compound index on %s — queries may be slow",
                self._base_collection,
                exc_info=True,
            )


    # ── Collection resolution (auto-provision) ────────────────────────────────

    def _collection_for_env(self, env: str):
        """
        Return the PyMongo Collection for the given env.

        When env_as_collection is true, each env gets its own collection.
        MongoDB creates it implicitly on first insert — no explicit setup.

        Safe characters: Mongo collection names cannot contain $ or NUL;
        branch slashes and dots are replaced with underscores.
        """
        safe = env.replace("/", "_").replace(".", "_").replace("$", "_").replace("\0", "_")
        if self._dest_tpl:
            col_name = resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=safe)
        elif self._env_as_collection:
            col_name = f"{self._base_collection}_{safe}"
        else:
            col_name = self._base_collection

        return self._db[col_name]

    # ── Write ─────────────────────────────────────────────────────────────────

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
        # Collection is auto-provisioned here for new envs (pr-42, v1.2.3 …)
        collection = self._collection_for_env(env)
        doc_filter = {
            "_vt_tenant":     self._tenant_id,
            "_vt_datasource": datasource,
            "_vt_env":        env,
        }
        meta = labels.as_mongo_meta()

        logger.debug(
            "MongoSink write env=%s collection=%s keys=%d",
            env, collection.name, len(flat_data),
        )

        if sync_mode == SyncMode.GIT_SMART_REPAIR:
            written = self._strategy.repair(
                collection, flat_data, doc_filter, commit_sha, file_path, meta
            )
        else:
            written = self._strategy.upsert(
                collection, flat_data, doc_filter, commit_sha, file_path, meta
            )

        pruned = 0
        if prune and not dry_run_prune:
            pruned = self._strategy.prune(
                collection, flat_data, doc_filter,
                protection=prune_protection, last=prune_last,
            )

        return {"written": written, "pruned": pruned}

    def delete_file_data(self, datasource: str, env: str, file_path: str) -> int:
        """
        Delete all documents written for a specific (datasource, env, file_path).

        Used by stage_sync to roll back files that were successfully written
        before a later file in the same batch failed.

        Returns the number of documents deleted.
        """
        collection = self._collection_for_env(env)
        doc_filter = {
            "_vt_tenant":     self._tenant_id,
            "_vt_datasource": datasource,
            "_vt_env":        env,
            "_vt_file":       file_path,
        }
        result = collection.delete_many(doc_filter)
        deleted = result.deleted_count
        logger.debug(
            "MongoSink.delete_file_data datasource=%s env=%s file=%s deleted=%d",
            datasource, env, file_path, deleted,
        )
        return deleted

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """Read current values for a set of field keys from MongoDB."""
        if not keys:
            return {}
        try:
            collection = self._collection_for_env(env)
            docs = collection.find(
                {
                    "_vt_tenant":     self._tenant_id,
                    "_vt_datasource": datasource,
                    "_vt_env":        env,
                    "key":            {"$in": list(keys)},
                },
                {"key": 1, "value": 1, "_id": 0},
            )
            return {doc["key"]: str(doc.get("value", "")) for doc in docs if "key" in doc}
        except Exception as exc:
            logger.warning("MongoSink.read_values failed: %s", exc)
            return {}
