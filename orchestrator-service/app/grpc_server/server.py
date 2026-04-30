"""
app/grpc_server/server.py

gRPC server that implements the vartrack.v1.Orchestrator service.

Receives webhooks from the Go gateway, dispatches Celery ETL tasks,
and returns task IDs.  Task status is retrievable via GetWebhookTask.

The server is intentionally thin — all business logic lives in Celery tasks.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent import futures
from typing import Any, Optional


def _m():
    try:
        from app.monitoring import get_metrics
        return get_metrics()
    except Exception:
        return None

try:
    import grpc
    from grpc import ServicerContext
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False
    grpc = None  # type: ignore

logger = logging.getLogger(__name__)

# Proto-generated stubs are expected to be on the Python path (installed as a package
# or added via PYTHONPATH). If not generated yet, the module degrades gracefully.
try:
    from vartrack.v1.services import webhook_pb2, webhook_pb2_grpc  # type: ignore
    PROTO_AVAILABLE = True
except ImportError:
    PROTO_AVAILABLE = False
    logger.warning(
        "Proto stubs not found — gRPC server will not start. "
        "Generate with: buf generate"
    )


class OrchestratorServicer:
    """
    Implements vartrack.v1.Orchestrator.

    The two primary RPCs:
      ProcessWebhook       – dispatches process_webhook_task
      ProcessSchemaWebhook – dispatches refresh_schema_task
      GetWebhookTask       – queries Celery backend for task state
      Ping                 – liveness check
    """

    def __init__(self, rule_registry: "dict | Any | None" = None) -> None:
        """
        rule_registry: optional callable/object that resolves rule_config from
        (platform, datasource).  In production this is loaded from the CUE
        bundle; in tests a dict works fine.
        """
        self._rule_registry = rule_registry or {}

    # ------------------------------------------------------------------
    # ProcessWebhook
    # ------------------------------------------------------------------

    def ProcessWebhook(self, request, context: "ServicerContext"):
        from app.tasks.etl import process_webhook_task
        from app.monitoring.tracer import start_grpc_span

        logger.info(
            "ProcessWebhook: platform=%s datasource=%s",
            request.platform, request.datasource,
        )

        _t0 = time.perf_counter()
        span = start_grpc_span("ProcessWebhook")

        rule_config = self._resolve_rule(request.platform, request.datasource)
        if rule_config is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(
                f"No rule configured for platform={request.platform} "
                f"datasource={request.datasource}"
            )
            span.set_attr("error", "no_rule").end()
            m = _m()
            if m:
                try:
                    m.inc_grpc_request("ProcessWebhook", "NOT_FOUND")
                    m.observe_grpc_duration("ProcessWebhook", time.perf_counter() - _t0)
                except Exception:
                    pass
            return _empty_webhook_response()

        task = process_webhook_task.apply_async(
            kwargs={
                "platform": request.platform,
                "datasource": request.datasource,
                "raw_payload": request.raw_payload,
                "headers": dict(request.headers),
                "received_at": _proto_ts_to_float(request.received_at),
                "rule_config": rule_config,
                "tenant_id": rule_config.get("tenant_id", "default"),
            },
            queue="webhooks",
        )

        span.set_attr("task_id", task.id).end()
        m = _m()
        if m:
            try:
                m.inc_grpc_request("ProcessWebhook", "OK")
                m.observe_grpc_duration("ProcessWebhook", time.perf_counter() - _t0)
            except Exception:
                pass

        logger.info("Dispatched process_webhook_task id=%s", task.id)
        return _webhook_response(task_id=task.id, message="accepted")

    # ------------------------------------------------------------------
    # ProcessSchemaWebhook
    # ------------------------------------------------------------------

    def ProcessSchemaWebhook(self, request, context: "ServicerContext"):
        from app.tasks.etl import refresh_schema_task

        logger.info(
            "ProcessSchemaWebhook: platform=%s repo=%s branch=%s",
            request.platform, request.repo, request.branch,
        )

        task = refresh_schema_task.apply_async(
            kwargs={
                "platform": request.platform,
                "repo": request.repo,
                "branch": request.branch,
                "raw_payload": request.raw_payload,
                "headers": dict(request.headers),
            },
            queue="schema",
        )

        logger.info("Dispatched refresh_schema_task id=%s", task.id)
        return _schema_response(task_id=task.id, message="schema accepted")

    # ------------------------------------------------------------------
    # GetWebhookTask
    # ------------------------------------------------------------------

    def GetWebhookTask(self, request, context: "ServicerContext"):
        """Query Celery backend for task state."""
        from celery.result import AsyncResult
        from app.worker.celery import celery

        result = AsyncResult(request.task_id, app=celery)
        state = _celery_state_to_proto(result.state)
        message = ""
        if result.state == "FAILURE":
            message = str(result.result)
        elif result.state == "SUCCESS" and isinstance(result.result, dict):
            message = result.result.get("message", "")

        return _task_response(
            task_id=request.task_id,
            state=state,
            message=message,
        )

    # ------------------------------------------------------------------
    # Ping
    # ------------------------------------------------------------------

    def Ping(self, request, context: "ServicerContext"):
        if PROTO_AVAILABLE:
            from google.protobuf import empty_pb2
            return empty_pb2.Empty()
        return None

    # ------------------------------------------------------------------
    # SyncDatasource  (called by watcher-service after drift detection)
    # ------------------------------------------------------------------

    def SyncDatasource(self, request, context: "ServicerContext"):
        """
        Enqueue a Celery sync_all_task for the given datasource/tenant.
        Returns immediately with the task_id; the watcher polls separately.
        """
        from app.tasks.etl import sync_all_task

        tenant_id = request.tenant or None
        logger.info(
            "SyncDatasource: datasource=%s platform=%s tenant=%s reason=%s",
            request.datasource, request.platform, tenant_id, request.reason,
        )

        task = sync_all_task.apply_async(
            kwargs={
                "rules":     [],       # loader resolves from CUE bundle at runtime
                "tenant_id": tenant_id,
            },
            queue="sync",
        )

        logger.info("SyncDatasource: enqueued sync_all_task id=%s", task.id)
        return _sync_response(task_id=task.id, status="accepted")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_rule(self, platform: str, datasource: str) -> Optional[dict]:
        """Resolve rule_config from registry. Supports dict or callable."""
        if callable(self._rule_registry):
            return self._rule_registry(platform, datasource)
        key = f"{platform}:{datasource}"
        return self._rule_registry.get(key) or self._rule_registry.get(datasource)


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def create_server(
    port: int = 50051,
    max_workers: int = 10,
    rule_registry: Optional[dict] = None,
) -> Optional[object]:
    """
    Create and return a gRPC server.
    Returns None if gRPC or proto stubs are not available.
    """
    if not GRPC_AVAILABLE:
        logger.error("grpcio not installed — cannot start gRPC server")
        return None
    if not PROTO_AVAILABLE:
        logger.error("Proto stubs missing — cannot start gRPC server")
        return None

    # OTel gRPC server interceptor: extracts W3C TraceContext from inbound gRPC
    # metadata so orchestrator spans become children of the gateway's spans.
    interceptors = []
    try:
        from opentelemetry.instrumentation.grpc import server_interceptor as otel_server_interceptor
        interceptors.append(otel_server_interceptor())
        logger.info("otelgrpc: server interceptor installed")
    except Exception as e:
        logger.warning("otelgrpc: server interceptor unavailable (%s) — spans will be unlinked", e)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=interceptors,
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),  # 10 MB
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
            ("grpc.keepalive_time_ms", 20_000),
            ("grpc.keepalive_timeout_ms", 5_000),
        ],
    )

    webhook_pb2_grpc.add_OrchestratorServicer_to_server(
        OrchestratorServicer(rule_registry), server
    )

    # Register the Watcher service (watcher-service → orchestrator self-heal RPC).
    try:
        from vartrack.v1.services import watcher_pb2_grpc  # type: ignore
        watcher_pb2_grpc.add_WatcherServicer_to_server(
            OrchestratorServicer(rule_registry), server
        )
    except ImportError:
        logger.warning("watcher proto stubs not found — SyncDatasource RPC unavailable; run: buf generate --template buf.gen.watcher.yaml && pip install -e .")

    # Use mTLS credentials when MTLS_ENABLED=true, otherwise insecure.
    # Insecure is the default so local dev and unit tests need no certs.
    try:
        from app.tls import build_grpc_server_credentials
        creds = build_grpc_server_credentials()
    except Exception:
        creds = None

    if creds is not None:
        server.add_secure_port(f"[::]:{port}", creds)
    else:
        server.add_insecure_port(f"[::]:{port}")

    return server


def run_server(port: int = 50051, max_workers: int = 10, rule_registry=None) -> None:
    """Blocking call: start the gRPC server and wait for termination."""
    import signal

    port = int(os.environ.get("GRPC_PORT", port))
    server = create_server(port=port, max_workers=max_workers, rule_registry=rule_registry)
    if server is None:
        logger.error("Server creation failed")
        return

    server.start()
    logger.info("gRPC Orchestrator server listening on :%d", port)

    # Signal handlers can only be set from the main thread.
    # When run_server runs in a daemon thread (the normal case), skip them —
    # uvicorn handles SIGTERM/SIGINT in the main thread and the daemon thread
    # is reaped automatically on process exit.
    import threading
    if threading.current_thread() is threading.main_thread():
        import signal

        def _stop(signum, frame):
            logger.info("Received signal %d, shutting down gRPC server", signum)
            server.stop(grace=10)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    server.wait_for_termination()


# ---------------------------------------------------------------------------
# Proto response builders (gracefully degrade when stubs missing)
# ---------------------------------------------------------------------------

def _empty_webhook_response():
    if PROTO_AVAILABLE:
        return webhook_pb2.ProcessWebhookResponse(task_id="", message="not_found")
    return type("R", (), {"task_id": "", "message": "not_found"})()


def _webhook_response(task_id: str, message: str):
    if PROTO_AVAILABLE:
        return webhook_pb2.ProcessWebhookResponse(task_id=task_id, message=message)
    return type("R", (), {"task_id": task_id, "message": message})()


def _schema_response(task_id: str, message: str):
    if PROTO_AVAILABLE:
        return webhook_pb2.ProcessSchemaWebhookResponse(task_id=task_id, message=message)
    return type("R", (), {"task_id": task_id, "message": message})()


def _task_response(task_id: str, state: int, message: str):
    if PROTO_AVAILABLE:
        return webhook_pb2.WebhookTask(task_id=task_id, state=state, message=message)
    return type("R", (), {"task_id": task_id, "state": state, "message": message})()


def _proto_ts_to_float(ts) -> float:
    try:
        return ts.seconds + ts.nanos / 1e9
    except Exception:
        return time.time()


def _sync_response(task_id: str, status: str):
    if PROTO_AVAILABLE:
        try:
            from vartrack.v1.services import watcher_pb2  # type: ignore
            return watcher_pb2.SyncDatasourceResponse(task_id=task_id, status=status)
        except ImportError:
            pass
    return type("R", (), {"task_id": task_id, "status": status})()


def _celery_state_to_proto(state: str) -> int:
    """Map Celery task state string to TaskState proto enum int."""
    mapping = {
        "PENDING":  1,  # TASK_STATE_PENDING
        "STARTED":  2,  # TASK_STATE_RUNNING
        "SUCCESS":  3,  # TASK_STATE_SUCCEEDED
        "FAILURE":  4,  # TASK_STATE_FAILED
        "RETRY":    5,  # TASK_STATE_RETRYING
        "REVOKED":  4,
    }
    return mapping.get(state, 0)
