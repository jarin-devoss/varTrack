"""
app/utils/enums/file_status.py
────────────────────────────────
FileStatus values mirror the proto enum one-to-one:

  proto/vartrack/v1/utils/enums.proto  →  enum FileStatus { ... }

  FILE_STATUS_UNSPECIFIED = 0
  FILE_STATUS_ADDED       = 1
  FILE_STATUS_MODIFIED    = 2
  FILE_STATUS_REMOVED     = 3
  FILE_STATUS_RENAMED     = 4
"""
from __future__ import annotations

from enum import IntEnum


try:
    from vartrack.v1.utils.enums_pb2 import (
        FILE_STATUS_UNSPECIFIED,
        FILE_STATUS_ADDED,
        FILE_STATUS_MODIFIED,
        FILE_STATUS_REMOVED,
        FILE_STATUS_RENAMED,
    )
except ImportError:
    FILE_STATUS_UNSPECIFIED = 0
    FILE_STATUS_ADDED = 1
    FILE_STATUS_MODIFIED = 2
    FILE_STATUS_REMOVED = 3
    FILE_STATUS_RENAMED = 4

class FileStatus(IntEnum):
    """Wire-compatible with vartrack.v1.FileStatus proto enum."""

    UNSPECIFIED = FILE_STATUS_UNSPECIFIED
    ADDED       = FILE_STATUS_ADDED
    MODIFIED    = FILE_STATUS_MODIFIED
    REMOVED     = FILE_STATUS_REMOVED
    RENAMED     = FILE_STATUS_RENAMED
