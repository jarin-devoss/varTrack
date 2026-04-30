"""
app/utils/enums/sync_mode.py
─────────────────────────────
SyncMode values mirror the proto enum one-to-one:

  proto/vartrack/v1/utils/enums.proto  →  enum SyncMode { ... }

  SYNC_MODE_UNSPECIFIED    = 0
  SYNC_MODE_GIT_UPSERT_ALL = 1
  SYNC_MODE_GIT_SMART_REPAIR = 2
  SYNC_MODE_LIVE_STATE     = 3
  SYNC_MODE_AUTO           = 4
"""
from __future__ import annotations

from enum import IntEnum


try:
    from vartrack.v1.utils.enums_pb2 import (
        SYNC_MODE_UNSPECIFIED,
        SYNC_MODE_GIT_UPSERT_ALL,
        SYNC_MODE_GIT_SMART_REPAIR,
        SYNC_MODE_LIVE_STATE,
        SYNC_MODE_AUTO,
    )
except ImportError:
    SYNC_MODE_UNSPECIFIED = 0
    SYNC_MODE_GIT_UPSERT_ALL = 1
    SYNC_MODE_GIT_SMART_REPAIR = 2
    SYNC_MODE_LIVE_STATE = 3
    SYNC_MODE_AUTO = 4

class SyncMode(IntEnum):
    """Wire-compatible with vartrack.v1.SyncMode proto enum."""

    UNSPECIFIED      = SYNC_MODE_UNSPECIFIED
    GIT_UPSERT_ALL   = SYNC_MODE_GIT_UPSERT_ALL
    GIT_SMART_REPAIR = SYNC_MODE_GIT_SMART_REPAIR
    LIVE_STATE       = SYNC_MODE_LIVE_STATE
    AUTO             = SYNC_MODE_AUTO
