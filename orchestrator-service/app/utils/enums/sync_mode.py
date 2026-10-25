"""
app/utils/enums/sync_mode.py
─────────────────────────────
SyncMode is the proto enum vartrack.v1.SyncMode (enums/sync_mode.proto),
built from its generated module — no values are written out here — under
the short member names the code uses:

  SYNC_MODE_UNSPECIFIED      → SyncMode.UNSPECIFIED
  SYNC_MODE_GIT_UPSERT_ALL   → SyncMode.GIT_UPSERT_ALL
  SYNC_MODE_GIT_SMART_REPAIR → SyncMode.GIT_SMART_REPAIR
  SYNC_MODE_LIVE_STATE       → SyncMode.LIVE_STATE
  SYNC_MODE_AUTO             → SyncMode.AUTO
"""
from __future__ import annotations

from vartrack.v1.enums import sync_mode_pb2

from app.utils.enums import proto_enum

_PREFIX = "SYNC_MODE_"
_PROTO = sync_mode_pb2.SyncMode

SyncMode = proto_enum("SyncMode", _PROTO, _PREFIX, __name__)


def parse_sync_mode(value: object) -> SyncMode:
    """A rule's sync_mode as written anywhere — the number, the proto's name
    ("SYNC_MODE_AUTO") or the short name ("AUTO", "git_smart_repair") — as
    the proto enum. "FULL" and SYNC_MODE_FULL, older names for a full upsert,
    read as GIT_UPSERT_ALL. Anything else is UNSPECIFIED, which syncs as
    AUTO does."""
    if isinstance(value, bool):
        return SyncMode.UNSPECIFIED
    if isinstance(value, int):
        return SyncMode(value) if value in _PROTO.values() else SyncMode.UNSPECIFIED
    if isinstance(value, str):
        text = value.strip().upper()
        if text.isdigit():
            return parse_sync_mode(int(text))
        name = _PREFIX + text.removeprefix(_PREFIX)
        if name == _PREFIX + "FULL":
            name = _PREFIX + "GIT_UPSERT_ALL"
        if name in _PROTO.keys():
            return SyncMode(_PROTO.Value(name))
    return SyncMode.UNSPECIFIED
