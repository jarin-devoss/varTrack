"""
app/pipeline/sinks/mongo.py
────────────────────────────
MongoDB sink.

Key format: DOT only
────────────────────
Mongo is a document store.  Dot-separated keys are natural JSON field names —
no path conversion is needed or allowed.

The shape follows the datasource's update_strategy (MongoConfig):

  STRATEGY_DOCUMENT (default) — one document per file, always nested:
    {
      "_vt_tenant": "acme", "_vt_datasource": "payments", "_vt_env": "pr-42",
      "data": {"db": {"host": "localhost", "port": 5432}}
    }

  STRATEGY_KEY_VALUE — one document per key:
    {
      "_vt_tenant": "acme", "_vt_datasource": "payments", "_vt_env": "pr-42",
      "key":   "db.host",
      "value": "localhost"
    }

  STRATEGY_FILE — one document per file, in the shape the file parsed to:
  nested (as above) for JSON, YAML, TOML …, flat for .env / .properties:
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

from app.pipeline.sinks.base import BaseSink, update_strategy
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import resolve_destination
from app.pipeline.sinks.strategies.mongo import get_strategy, MongoWriteStrategy
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_DEFAULT_DB         = "vartrack"
_DEFAULT_COLLECTION = "configs"


class MongoSink(BaseSink):
    object_name = "document"

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

        # MongoConfig.update_strategy: STRATEGY_DOCUMENT (default), STRATEGY_KEY_VALUE or STRATEGY_FILE.
        strategy_name = cfg.get("update_strategy")
        self._strategy: MongoWriteStrategy = get_strategy(strategy_name)
        self._strategy_name = update_strategy(strategy_name) if strategy_name else "STRATEGY_DOCUMENT"

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
        total_sources: int,
        data: Any = None,
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
            result = self._strategy.repair(
                collection, flat_data, doc_filter, commit_sha, file_path, meta, data=data
            )
        else:
            result = self._strategy.upsert(
                collection, flat_data, doc_filter, commit_sha, file_path, meta, data=data
            )

        if result.replaced:
            # The datasource's update_strategy changed since this file was last
            # written: what the old strategy stored for it is gone.
            logger.warning(
                "MongoSink: update_strategy %s replaced %d document%s of the previous shape "
                "tenant=%s datasource=%s env=%s file=%s",
                self._strategy_name, result.replaced, "" if result.replaced == 1 else "s",
                self._tenant_id, datasource, env, file_path,
            )
        return {"written": result.written, "replaced": result.replaced, "strategy": self._strategy_name}

    # ── Prune ─────────────────────────────────────────────────────────────────

    @classmethod
    def prunes_keys(cls, rule_config: dict) -> bool:
        # Only the key-value strategy keeps one document per key, and only when it
        # is asked for: the default, like the file strategy, replaces the file's
        # document on every write.
        try:
            return update_strategy(rule_config.get("update_strategy")) == "STRATEGY_KEY_VALUE"
        except ValueError:
            return False

    @classmethod
    def key_space(cls, rule_config: dict, tenant_id: str, env: str) -> str:
        # Documents are filtered by tenant, datasource and env: environments
        # never share keys, even in one collection.
        return f"{tenant_id}/{rule_config.get('datasource', '')}/{env}"

    def delete_keys(self, keys: "Collection[str]", *, datasource: str, env: str) -> int:
        if not keys:
            return 0
        from pymongo import DeleteOne

        doc_filter = {"_vt_tenant": self._tenant_id, "_vt_datasource": datasource, "_vt_env": env}
        result = self._collection_for_env(env).bulk_write(
            [DeleteOne({**doc_filter, "key": key}) for key in keys], ordered=False)
        return result.deleted_count

    def snapshot_file_data(self, datasource: str, env: str, file_path: str) -> list[dict]:
        """
        Return a copy of every document currently stored for (datasource, env, file_path).

        The returned list can be passed to restore_file_data to undo a write.
        Returns an empty list when no documents exist yet (first write to this file).
        """
        collection = self._collection_for_env(env)
        doc_filter = {
            "_vt_tenant":     self._tenant_id,
            "_vt_datasource": datasource,
            "_vt_env":        env,
            "_vt_file":       file_path,
        }
        try:
            docs = list(collection.find(doc_filter, {"_id": 0}))
        except Exception as exc:
            logger.warning(
                "MongoSink.snapshot_file_data failed datasource=%s env=%s file=%s: %s",
                datasource, env, file_path, exc,
            )
            docs = []
        logger.debug(
            "MongoSink.snapshot_file_data datasource=%s env=%s file=%s docs=%d",
            datasource, env, file_path, len(docs),
        )
        return docs

    def restore_file_data(self, datasource: str, env: str, file_path: str, snapshot: list[dict]) -> None:
        """
        Replace the current documents for (datasource, env, file_path) with snapshot.

        Deletes everything currently stored for the file, then re-inserts the
        snapshot documents.  When snapshot is empty the file is left absent,
        which is correct for a first-write rollback.
        """
        collection = self._collection_for_env(env)
        doc_filter = {
            "_vt_tenant":     self._tenant_id,
            "_vt_datasource": datasource,
            "_vt_env":        env,
            "_vt_file":       file_path,
        }
        try:
            collection.delete_many(doc_filter)
            if snapshot:
                collection.insert_many(snapshot)
            logger.debug(
                "MongoSink.restore_file_data datasource=%s env=%s file=%s restored=%d",
                datasource, env, file_path, len(snapshot),
            )
        except Exception:
            logger.exception(
                "MongoSink.restore_file_data failed datasource=%s env=%s file=%s",
                datasource, env, file_path,
            )

    def delete_file_data(self, datasource: str, env: str, file_path: str) -> int:
        """
        Delete all documents written for a specific (datasource, env, file_path).

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
            doc_filter = {"_vt_tenant": self._tenant_id, "_vt_datasource": datasource, "_vt_env": env}
            # Each strategy stores the keys its own way: one document per key, or one per file.
            return self._strategy.read_values(collection, list(keys), doc_filter)
        except Exception as exc:
            return self._read_failed(exc, "MongoSink.read_values")
