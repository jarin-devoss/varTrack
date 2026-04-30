"""
app/pipeline/sinks/strategies/mongo/__init__.py
─────────────────────────────────────────────────
Strategy factory for MongoDB write modes.

  "document"  — one doc per flat key  (default)
  "file"      — one doc holds entire flat_data dict
"""
from __future__ import annotations

from app.pipeline.sinks.strategies.mongo.base import MongoWriteStrategy
from app.pipeline.sinks.strategies.mongo.document import DocumentStrategy
from app.pipeline.sinks.strategies.mongo.file import FileStrategy

__all__ = ["MongoWriteStrategy", "DocumentStrategy", "FileStrategy", "get_strategy"]


def get_strategy(name: str) -> MongoWriteStrategy:
    strategies = {
        "document": DocumentStrategy,
        "file":     FileStrategy,
    }
    cls = strategies.get(name.lower())
    if cls is None:
        raise ValueError(
            f"Unknown mongo_strategy '{name}'. Available: {list(strategies)}"
        )
    return cls()
