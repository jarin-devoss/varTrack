"""
app/utils/enums/task_state.py
──────────────────────────────
TaskState represents the lifecycle state of a Celery task as returned
to gRPC callers.  Values are intentionally aligned with the Celery state
strings and the proto TaskState mapping in grpc_server/server.py.

Proto mapping (grpc_server/server.py::_celery_state_to_proto):
  UNSPECIFIED = 0  →  unknown / initial
  PENDING     = 1  →  TASK_STATE_PENDING   (Celery: "PENDING")
  RUNNING     = 2  →  TASK_STATE_RUNNING   (Celery: "STARTED")
  SUCCEEDED   = 3  →  TASK_STATE_SUCCEEDED (Celery: "SUCCESS")
  FAILED      = 4  →  TASK_STATE_FAILED    (Celery: "FAILURE" / "REVOKED")
  RETRYING    = 5  →  TASK_STATE_RETRYING  (Celery: "RETRY")
"""
from __future__ import annotations

from enum import IntEnum


class TaskState(IntEnum):
    UNSPECIFIED = 0
    PENDING     = 1
    RUNNING     = 2
    SUCCEEDED   = 3
    FAILED      = 4
    RETRYING    = 5
