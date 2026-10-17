"""
app/pipeline/sinks/strategies/mongo/document.py
─────────────────────────────────────────────────
DocumentStrategy — the config as one nested document (STRATEGY_DOCUMENT): one
MongoDB document per environment and file, "data" always nested. It is the
tree the ETL cut from the file (root_key, environment), so arrays and value
types are kept; a flat file (.env, .properties) is nested by its dotted keys.

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
        "database": {"host": "prod-db.internal", "port": 5432},
        "cache":    {"ttl": 300, "regions": ["eu", "us"]},
    }
}

Reading a value: db.configs.findOne({_vt_env: "production"}).data.database.host

Writing, pruning and reading back work as for FileStrategy: the document is
replaced on every write, so keys removed from Git disappear with it.
"""
from __future__ import annotations

from typing import Any

from app.pipeline.sinks.strategies.mongo.file import FileStrategy
from app.pipeline.transformer import as_nested


class DocumentStrategy(FileStrategy):

    @staticmethod
    def _shape(flat_data: dict, data: Any) -> Any:
        # The file's tree; flat keys (.env, .properties) are nested by their dots.
        return as_nested(data if data is not None else flat_data)
