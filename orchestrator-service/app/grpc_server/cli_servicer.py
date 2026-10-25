"""
app/grpc_server/cli_servicer.py

Implements vartrack.v1.CLIService — what the gateway calls for the CLI API
(/v1/cli/*) once a request has passed its OIDC, RBAC and OPA checks.

The gateway passes the caller in gRPC metadata: x-cli-user (email or
subject, recorded with the task) and x-tenant-id (when the request names
no tenant). The operations themselves live in app.cli_ops.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ServicerBase: Any  # the generated base class, or object without the stubs
try:
    import grpc
    from vartrack.v1.enums import task_state_pb2  # type: ignore
    from vartrack.v1.services import cli_pb2, cli_pb2_grpc  # type: ignore
    CLI_PROTO_AVAILABLE = True
    _ServicerBase = cli_pb2_grpc.CLIServiceServicer
except ImportError:  # stubs not generated — create_server skips this service
    CLI_PROTO_AVAILABLE = False
    _ServicerBase = object


def _metadata(context) -> dict[str, str]:
    try:
        return {k.lower(): v for k, v in (context.invocation_metadata() or ())}
    except Exception:
        return {}


def _state(name: str) -> int:
    try:
        return task_state_pb2.TaskState.Value(f"TASK_STATE_{name}")
    except ValueError:
        return task_state_pb2.TaskState.Value("TASK_STATE_UNSPECIFIED")


def _set_time(field, iso: str) -> None:
    if iso:
        try:
            field.FromJsonString(iso)
        except ValueError:
            logger.debug("cli task: unreadable timestamp %r", iso)


def _task_message(view: dict[str, Any], tenant_id: str = ""):
    task = cli_pb2.CliTask(
        task_id=view["task_id"],
        state=_state(view["state"]),
        message=view["message"],
        datasource=view["datasource"],
        platform=view.get("platform") or "",
        env=view["env"],
        file_path=view["file_path"],
        tenant_id=view.get("tenant_id") or tenant_id,
        dry_run=view["dry_run"],
        label=view["label"],
        written=view["written"],
        pruned=view["pruned"],
        error=view["error"],
    )
    _set_time(task.created_at, view["created_at"])
    _set_time(task.finished_at, view["finished_at"])
    return task


class CLIServicer(_ServicerBase):
    """vartrack.v1.CLIService: sync a file, validate a file, report on tasks."""

    def SyncFile(self, request, context):
        from app import cli_ops

        # A revision syncs the rule's own file from Git, so it comes without one.
        needed = ("datasource", "env") if request.revision else ("datasource", "env", "file_path", "content")
        missing = [f for f in needed if not getattr(request, f)]
        if missing:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"required: {', '.join(missing)}")
        if request.revision and (request.file_path or request.content):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "a revision syncs the rule's file from Git: send no file")
        if request.revision and not cli_ops.valid_revision(request.revision):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"not a branch or tag name: {request.revision!r}")

        md = _metadata(context)
        try:
            task_id = cli_ops.enqueue_sync(
                datasource=request.datasource,
                env=request.env,
                file_path=request.file_path,
                content=request.content,
                fmt=cli_ops.detect_format(request.file_path, request.format),
                tenant_id=cli_ops.resolve_tenant(request.tenant_id, md.get("x-tenant-id")),
                dry_run=request.dry_run,
                label=request.label,
                submitted_by=md.get("x-cli-user") or "unknown",
                revision=request.revision,
                platform=request.platform,
            )
        except Exception as exc:
            logger.exception("CLIService.SyncFile: failed to enqueue")
            context.abort(grpc.StatusCode.UNAVAILABLE, f"could not queue the sync: {exc}")

        return cli_pb2.SyncFileResponse(task_id=task_id, message="sync task enqueued", dry_run=request.dry_run)

    def ValidateFile(self, request, context):
        from app import cli_ops

        if not request.file_path or not request.content:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "required: file_path, content")

        status, messages, key_count = cli_ops.validate_content(
            file_path=request.file_path,
            content=request.content,
            fmt=cli_ops.detect_format(request.file_path, request.format),
            datasource=request.datasource or None,
            tenant_id=cli_ops.resolve_tenant(request.tenant_id, _metadata(context).get("x-tenant-id")),
            platform=request.platform,
        )
        return cli_pb2.ValidateFileResponse(status=status, messages=messages, key_count=key_count)

    def ValidateConfig(self, request, context):
        from app import cli_ops, config_validator

        if not request.file_path or not request.content:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "required: file_path, content")

        result = config_validator.validate(
            request.file_path,
            request.content,
            cli_ops.resolve_tenant(request.tenant_id, _metadata(context).get("x-tenant-id")),
        )
        return cli_pb2.ValidateConfigResponse(
            errors=result.errors, warnings=result.warnings, migrations=result.migrations)

    def GetTask(self, request, context):
        from app import cli_ops

        if not request.task_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "required: task_id")
        return _task_message(cli_ops.task_view(request.task_id))

    def ListTasks(self, request, context):
        from app import cli_ops

        tenant_id = cli_ops.resolve_tenant(request.tenant_id, _metadata(context).get("x-tenant-id"))
        limit = request.limit if 0 < request.limit <= 200 else 20
        try:
            views = cli_ops.list_tasks(tenant_id, limit=limit)
        except Exception as exc:
            logger.warning("CLIService.ListTasks: task ledger unavailable: %s", exc)
            context.abort(grpc.StatusCode.UNAVAILABLE, f"task ledger unavailable: {exc}")
        return cli_pb2.ListCliTasksResponse(tasks=[_task_message(v, tenant_id) for v in views])
