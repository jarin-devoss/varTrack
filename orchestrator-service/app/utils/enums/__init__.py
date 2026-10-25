"""
app/utils/enums
───────────────
The proto enums as Python IntEnums, built from their generated modules so
the values are never written out twice: vartrack.v1.TaskState's
TASK_STATE_FAILED is TaskState.FAILED here, with the same number.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any


def proto_enum(name: str, proto: Any, prefix: str, module: str) -> Any:
    """An IntEnum named *name* with the members of the generated proto enum
    *proto*, each under its name without *prefix*."""
    return IntEnum(name, {n.removeprefix(prefix): v for n, v in proto.items()}, module=module)
