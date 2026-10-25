"""
app/utils/enums/file_status.py
───────────────────────────────
FileStatus is the proto enum vartrack.v1.FileStatus (enums/file_status.proto):
UNSPECIFIED, ADDED, MODIFIED, REMOVED, RENAMED.
"""
from __future__ import annotations

from vartrack.v1.enums import file_status_pb2

from app.utils.enums import proto_enum

FileStatus = proto_enum("FileStatus", file_status_pb2.FileStatus, "FILE_STATUS_", __name__)
