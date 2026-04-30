"""
app/utils/enums/apply_strategy.py
───────────────────────────────────
ApplyStrategy values mirror the proto enum one-to-one:

  proto/vartrack/v1/utils/enums.proto  →  enum ApplyStrategy { ... }

  APPLY_STRATEGY_UNSPECIFIED  = 0
  APPLY_STRATEGY_CLIENT_SIDE  = 1

Semantics
─────────
CLIENT_SIDE  – The orchestrator (this service) decides how and when to apply
               the sync strategy.  This is the only supported mode.
"""
from __future__ import annotations

from enum import IntEnum


try:
    from vartrack.v1.utils.enums_pb2 import (
        APPLY_STRATEGY_UNSPECIFIED,
        APPLY_STRATEGY_CLIENT_SIDE,
    )
except ImportError:
    APPLY_STRATEGY_UNSPECIFIED = 0
    APPLY_STRATEGY_CLIENT_SIDE = 1


class ApplyStrategy(IntEnum):
    """Wire-compatible with vartrack.v1.ApplyStrategy proto enum."""

    UNSPECIFIED = APPLY_STRATEGY_UNSPECIFIED
    CLIENT_SIDE = APPLY_STRATEGY_CLIENT_SIDE
