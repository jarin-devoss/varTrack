"""
app/utils/enums/task_state.py
──────────────────────────────
TaskState is the proto enum vartrack.v1.TaskState (enums/task_state.proto):
the lifecycle state of a Celery task as returned to gRPC callers.

  TaskState.UNSPECIFIED  unknown / initial
  TaskState.PENDING      Celery "PENDING"
  TaskState.RUNNING      Celery "STARTED"
  TaskState.SUCCEEDED    Celery "SUCCESS"
  TaskState.FAILED       Celery "FAILURE" / "REVOKED"
  TaskState.RETRYING     Celery "RETRY"
"""
from __future__ import annotations

from vartrack.v1.enums import task_state_pb2

from app.utils.enums import proto_enum

TaskState = proto_enum("TaskState", task_state_pb2.TaskState, "TASK_STATE_", __name__)
