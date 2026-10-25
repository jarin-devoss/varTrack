"""
app/pipeline/sinks/strategies/mongo/__init__.py
─────────────────────────────────────────────────
MongoDB write strategies, chosen by the datasource's update_strategy
(MongoConfig.update_strategy, the proto Strategy enum):

  STRATEGY_DOCUMENT   — one document per file, always nested         (default)
  STRATEGY_KEY_VALUE  — one document per flat key
  STRATEGY_FILE       — one document per file, in the shape it parsed to
"""
from __future__ import annotations

from collections.abc import Callable

from app.pipeline.sinks.base import update_strategy
from app.pipeline.sinks.strategies.mongo.base import MongoWriteStrategy
from app.pipeline.sinks.strategies.mongo.document import DocumentStrategy
from app.pipeline.sinks.strategies.mongo.file import FileStrategy
from app.pipeline.sinks.strategies.mongo.key_value import KeyValueStrategy

__all__ = ["MongoWriteStrategy", "KeyValueStrategy", "DocumentStrategy", "FileStrategy", "get_strategy"]

# By the proto Strategy enum's value names.
_STRATEGIES: dict[str, Callable[[], MongoWriteStrategy]] = {
    "STRATEGY_UNSPECIFIED": DocumentStrategy,
    "STRATEGY_KEY_VALUE":   KeyValueStrategy,
    "STRATEGY_DOCUMENT":    DocumentStrategy,
    "STRATEGY_FILE":        FileStrategy,
}


def get_strategy(value: str | int | None) -> MongoWriteStrategy:
    """The strategy for a datasource's update_strategy (name or proto number); unset is STRATEGY_DOCUMENT."""
    try:
        return _STRATEGIES[update_strategy(value)]()
    except ValueError:
        available = [name for name in _STRATEGIES if name != "STRATEGY_UNSPECIFIED"]
        raise ValueError(f"MongoDB has no update_strategy {value!r}. Available: {available}") from None
