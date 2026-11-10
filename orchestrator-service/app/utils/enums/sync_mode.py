"""
app/utils/enums/sync_mode.py
─────────────────────────────
SyncMode is the proto enum vartrack.v1.SyncMode (enums/sync_mode.proto),
built from its generated module — no values are written out here — under
the short member names the code uses:

  SYNC_MODE_UNSPECIFIED      → SyncMode.UNSPECIFIED
  SYNC_MODE_GIT_UPSERT_ALL   → SyncMode.GIT_UPSERT_ALL
  SYNC_MODE_GIT_SMART_REPAIR → SyncMode.GIT_SMART_REPAIR
  SYNC_MODE_AUTO             → SyncMode.AUTO

A sync_mode that is none of these is refused (sync_mode_problem says why); it is never read as AUTO.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from vartrack.v1.enums import sync_mode_pb2

from app.utils.enums import proto_enum

_PREFIX = "SYNC_MODE_"
_PROTO = sync_mode_pb2.SyncMode

# The modes a rule can name, in the order every message lists them.
MODES = ("AUTO", "GIT_UPSERT_ALL", "GIT_SMART_REPAIR")

if TYPE_CHECKING:
    # built at run time from the generated module: not a class the checker can see
    SyncMode = Any
else:
    SyncMode = proto_enum("SyncMode", _PROTO, _PREFIX, __name__)


def _read(value: object) -> "SyncMode | None":
    """*value* as the proto enum, or None when it is not one of the proto's values."""
    if value is None:
        return SyncMode.UNSPECIFIED
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return SyncMode(value) if value in _PROTO.values() else None
    if isinstance(value, str):
        text = value.strip().upper()
        if not text:
            return SyncMode.UNSPECIFIED
        if text.isdigit():
            return _read(int(text))
        name = _PREFIX + text.removeprefix(_PREFIX)
        if name in _PROTO.keys():
            return SyncMode(_PROTO.Value(name))
    return None


def sync_mode_problem(value: object) -> str:
    """Why *value* is not a sync_mode, or "" when it is: absent, empty, 0, or one of AUTO, GIT_UPSERT_ALL and
    GIT_SMART_REPAIR — by short name, by proto name (SYNC_MODE_AUTO), in any case, or by number. Anything else, a
    name varTrack once had (LIVE_STATE, FULL) included, is refused rather than read as AUTO."""
    if _read(value) is not None:
        return ""
    shown = json.dumps(value, default=str)
    return f"sync_mode {shown} is not a mode: use {', '.join(MODES[:-1])} or {MODES[-1]}"


def parse_sync_mode(value: object) -> SyncMode:
    """A rule's sync_mode as written anywhere — the number, the proto's name ("SYNC_MODE_AUTO") or the short name
    ("AUTO", "git_smart_repair") — as the proto enum. An absent or empty value is UNSPECIFIED, which syncs as AUTO
    does. Anything that is not a mode raises ValueError with sync_mode_problem's sentence."""
    mode = _read(value)
    if mode is None:
        raise ValueError(sync_mode_problem(value))
    return mode
