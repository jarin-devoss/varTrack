"""
app/pipeline/datasource_fields.py
─────────────────────────────────
Which fields of a datasource block no sink reads yet.

The protos declare every setting a datasource block may hold, and the gateway accepts
them all. A sink reads only some. A field no service reads says so on the field itself
((vartrack.v1.config).unused_reason), instead of being dropped without a word: set in
config.cue it has no effect, and the log says that once per datasource and field.
tests/unit/test_config_contract.py fails for a field that is neither read nor explained,
and for one that is explained but now read.
"""
from __future__ import annotations

import logging

from vartrack.v1 import options_pb2
from vartrack.v1.models import datasource_pb2

logger = logging.getLogger(__name__)


def _unused() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for field in datasource_pb2.DataSource.DESCRIPTOR.fields:
        if field.message_type is None:
            continue
        reasons = {
            f.name: f.GetOptions().Extensions[options_pb2.config].unused_reason
            for f in field.message_type.fields
            if f.GetOptions().Extensions[options_pb2.config].unused_reason
        }
        if reasons:
            out[field.name] = reasons
    return out


# datasource kind ("mongo", "redis", …) → field → why no sink reads it
UNUSED: dict[str, dict[str, str]] = _unused()

_warned: set[tuple[str, str]] = set()


def warn_unused(kind: str, cfg: dict, datasource: str) -> list[str]:
    """Say, once, which fields *cfg* sets that have no effect. Returns them."""
    unused = UNUSED.get(kind, {})
    ignored = sorted(name for name in cfg if name in unused)
    for name in ignored:
        if (datasource, name) not in _warned:
            _warned.add((datasource, name))
            logger.warning("datasource %s: %s is declared but %s", datasource, name, unused[name])
    return ignored
