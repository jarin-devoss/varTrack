"""
app/monitoring/metrics.py
──────────────────────────
OrchestratorMetrics — central Prometheus registry for the orchestrator service.

Design: typed Inc/Observe/Set methods so call-sites never touch prometheus
internals directly.

Non-global registry:
    CollectorRegistry() instead of the default global REGISTRY — avoids
    test-time conflicts and makes the registry injectable.

All metrics share the "orch_" namespace following Prometheus naming conventions.
"""
from __future__ import annotations

import threading


class OrchestratorMetrics:
    """Central metrics registry for the orchestrator service."""

    # Singleton — created once by init(), shared everywhere.
    _instance: OrchestratorMetrics | None = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        try:
            from prometheus_client import (
                CollectorRegistry,
                Counter,
                Gauge,
                Histogram,
            )
        except ImportError as exc:
            raise RuntimeError(
                "prometheus_client is required: pip install prometheus-client"
            ) from exc

        # Non-global registry — avoids test-time conflicts with the default global REGISTRY.
        self._registry = CollectorRegistry(auto_describe=True)

        # ── HTTP layer ─────────────────────────────────────────────────────────

        self._http_requests_total = Counter(
            "orch_http_requests_total",
            "Total HTTP requests received, by method, path, and status code.",
            ["method", "path", "status_code"],
            registry=self._registry,
        )
        self._http_request_duration_seconds = Histogram(
            "orch_http_request_duration_seconds",
            "End-to-end HTTP request latency.",
            ["method", "path"],
            buckets=[.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10],
            registry=self._registry,
        )
        self._http_request_body_bytes = Histogram(
            "orch_http_request_body_bytes",
            "Inbound HTTP request body size in bytes.",
            ["path"],
            buckets=[256, 1024, 4096, 16_384, 65_536, 262_144, 1_048_576, 4_194_304],
            registry=self._registry,
        )
        self._http_active_requests = Gauge(
            "orch_http_active_requests",
            "Number of currently in-flight HTTP requests.",
            ["method", "path"],
            registry=self._registry,
        )

        # ── Webhook processing (inbound from gateway gRPC) ─────────────────────

        self._webhooks_total = Counter(
            "orch_webhooks_total",
            "Total webhooks dispatched to Celery, by datasource, platform, and outcome.",
            ["datasource", "platform", "outcome"],
            registry=self._registry,
        )
        self._webhook_dispatch_duration_seconds = Histogram(
            "orch_webhook_dispatch_duration_seconds",
            "Time from gRPC receipt to Celery task enqueue.",
            ["datasource", "platform"],
            buckets=[.001, .005, .01, .025, .05, .1, .25, .5, 1],
            registry=self._registry,
        )

        # ── Celery task metrics ────────────────────────────────────────────────

        self._task_duration_seconds = Histogram(
            "orch_task_duration_seconds",
            "Celery task end-to-end execution time in seconds.",
            ["task_name", "queue"],
            buckets=[.1, .5, 1, 2.5, 5, 10, 30, 60, 120, 300],
            registry=self._registry,
        )
        self._task_total = Counter(
            "orch_tasks_total",
            "Total Celery task executions, by task name, queue, and status.",
            ["task_name", "queue", "status"],   # status: success|failure|retry|dlq
            registry=self._registry,
        )
        self._task_retries_total = Counter(
            "orch_task_retries_total",
            "Total Celery task retries, by task name.",
            ["task_name"],
            registry=self._registry,
        )
        self._task_queue_depth = Gauge(
            "orch_task_queue_depth",
            "Approximate number of messages in each Celery queue.",
            ["queue"],
            registry=self._registry,
        )

        # ── ETL pipeline stage metrics ─────────────────────────────────────────

        self._etl_stage_duration_seconds = Histogram(
            "orch_etl_stage_duration_seconds",
            "Duration of each ETL pipeline stage in seconds.",
            ["stage"],   # payload | etl | sync
            buckets=[.01, .05, .1, .5, 1, 5, 10, 30, 60],
            registry=self._registry,
        )
        self._etl_files_total = Counter(
            "orch_etl_files_total",
            "Total files processed by ETL, by stage outcome.",
            ["outcome"],   # ok | empty | skipped | error | validation_failed | aborted
            registry=self._registry,
        )
        self._etl_keys_total = Counter(
            "orch_etl_keys_total",
            "Total flat keys extracted across all ETL runs.",
            registry=self._registry,
        )

        # ── MongoDB sink metrics ───────────────────────────────────────────────

        self._mongo_write_duration_seconds = Histogram(
            "orch_mongo_write_duration_seconds",
            "MongoDB write operation duration in seconds.",
            ["sync_mode"],   # GIT_UPSERT_ALL | GIT_SMART_REPAIR
            buckets=[.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5],
            registry=self._registry,
        )
        self._mongo_documents_written_total = Counter(
            "orch_mongo_documents_written_total",
            "Total key-value documents written to MongoDB.",
            ["datasource", "sync_mode"],
            registry=self._registry,
        )
        self._mongo_documents_pruned_total = Counter(
            "orch_mongo_documents_pruned_total",
            "Total documents pruned from MongoDB (stale keys removed).",
            ["datasource"],
            registry=self._registry,
        )
        self._mongo_errors_total = Counter(
            "orch_mongo_errors_total",
            "Total MongoDB write/prune errors.",
            ["operation"],   # write | prune | rollback
            registry=self._registry,
        )

        # ── gRPC server metrics (inbound from gateway) ─────────────────────────

        self._grpc_requests_total = Counter(
            "orch_grpc_requests_total",
            "Total inbound gRPC requests, by method and status.",
            ["rpc_method", "status"],
            registry=self._registry,
        )
        self._grpc_request_duration_seconds = Histogram(
            "orch_grpc_request_duration_seconds",
            "Inbound gRPC request handling duration in seconds.",
            ["rpc_method"],
            buckets=[.001, .005, .01, .025, .05, .1, .25, .5, 1, 2.5],
            registry=self._registry,
        )

        # ── Schema registry metrics ────────────────────────────────────────────

        self._schema_operations_total = Counter(
            "orch_schema_operations_total",
            "Total schema registry operations, by operation and outcome.",
            ["operation", "outcome"],   # operation: get|clone|invalidate; outcome: hit|miss|error
            registry=self._registry,
        )
        self._schema_clone_duration_seconds = Histogram(
            "orch_schema_clone_duration_seconds",
            "Duration of schema repo clone/pull operations in seconds.",
            ["tenant_id"],
            buckets=[.5, 1, 2.5, 5, 10, 30, 60],
            registry=self._registry,
        )

        # ── Git extractor metrics ──────────────────────────────────────────────

        self._git_fetch_duration_seconds = Histogram(
            "orch_git_fetch_duration_seconds",
            "Duration of git file fetch operations in seconds.",
            ["outcome"],   # ok | error | cache_hit
            buckets=[.05, .1, .25, .5, 1, 2.5, 5, 10, 30],
            registry=self._registry,
        )

        # ── Build / process info ───────────────────────────────────────────────

        self._build_info = Gauge(
            "orch_build_info",
            "Orchestrator service build information. Always 1.",
            ["version", "commit", "python_version"],
            registry=self._registry,
        )

    # ── HTTP methods ──────────────────────────────────────────────────────────

    def inc_http_request(self, method: str, path: str, status_code: str) -> None:
        self._http_requests_total.labels(method, path, status_code).inc()

    def observe_http_duration(self, method: str, path: str, seconds: float) -> None:
        self._http_request_duration_seconds.labels(method, path).observe(seconds)

    def observe_http_body_bytes(self, path: str, nbytes: float) -> None:
        self._http_request_body_bytes.labels(path).observe(nbytes)

    def inc_active_requests(self, method: str, path: str) -> None:
        self._http_active_requests.labels(method, path).inc()

    def dec_active_requests(self, method: str, path: str) -> None:
        self._http_active_requests.labels(method, path).dec()

    # ── Webhook methods ───────────────────────────────────────────────────────

    def inc_webhook(self, datasource: str, platform: str, outcome: str) -> None:
        self._webhooks_total.labels(datasource, platform, outcome).inc()

    def observe_webhook_dispatch(self, datasource: str, platform: str, seconds: float) -> None:
        self._webhook_dispatch_duration_seconds.labels(datasource, platform).observe(seconds)

    # ── Celery task methods ───────────────────────────────────────────────────

    def observe_task_duration(self, task_name: str, queue: str, seconds: float) -> None:
        self._task_duration_seconds.labels(task_name, queue).observe(seconds)

    def inc_task(self, task_name: str, queue: str, status: str) -> None:
        self._task_total.labels(task_name, queue, status).inc()

    def inc_task_retry(self, task_name: str) -> None:
        self._task_retries_total.labels(task_name).inc()

    def set_queue_depth(self, queue: str, depth: float) -> None:
        self._task_queue_depth.labels(queue).set(depth)

    # ── ETL pipeline methods ──────────────────────────────────────────────────

    def observe_etl_stage(self, stage: str, seconds: float) -> None:
        self._etl_stage_duration_seconds.labels(stage).observe(seconds)

    def inc_etl_file(self, outcome: str) -> None:
        self._etl_files_total.labels(outcome).inc()

    def inc_etl_keys(self, count: int) -> None:
        self._etl_keys_total.inc(count)

    # ── MongoDB methods ───────────────────────────────────────────────────────

    def observe_mongo_write(self, sync_mode: str, seconds: float) -> None:
        self._mongo_write_duration_seconds.labels(sync_mode).observe(seconds)

    def inc_mongo_written(self, datasource: str, sync_mode: str, count: int) -> None:
        if count > 0:
            self._mongo_documents_written_total.labels(datasource, sync_mode).inc(count)

    def inc_mongo_pruned(self, datasource: str, count: int) -> None:
        if count > 0:
            self._mongo_documents_pruned_total.labels(datasource).inc(count)

    def inc_mongo_error(self, operation: str) -> None:
        self._mongo_errors_total.labels(operation).inc()

    # ── gRPC methods ──────────────────────────────────────────────────────────

    def inc_grpc_request(self, rpc_method: str, status: str) -> None:
        self._grpc_requests_total.labels(rpc_method, status).inc()

    def observe_grpc_duration(self, rpc_method: str, seconds: float) -> None:
        self._grpc_request_duration_seconds.labels(rpc_method).observe(seconds)

    # ── Schema registry methods ───────────────────────────────────────────────

    def inc_schema_op(self, operation: str, outcome: str) -> None:
        self._schema_operations_total.labels(operation, outcome).inc()

    def observe_schema_clone(self, tenant_id: str, seconds: float) -> None:
        self._schema_clone_duration_seconds.labels(tenant_id).observe(seconds)

    # ── Git extractor methods ─────────────────────────────────────────────────

    def observe_git_fetch(self, outcome: str, seconds: float) -> None:
        self._git_fetch_duration_seconds.labels(outcome).observe(seconds)

    # ── Build info ────────────────────────────────────────────────────────────

    def set_build_info(self, version: str, commit: str, python_version: str) -> None:
        self._build_info.labels(version, commit, python_version).set(1)

    # ── /metrics endpoint helper ──────────────────────────────────────────────

    def generate_latest(self) -> bytes:
        """Return the current metrics in text exposition format."""
        from prometheus_client import generate_latest
        return generate_latest(self._registry)

    def generate_latest_openmetrics(self) -> bytes:
        """Return metrics in OpenMetrics format."""
        from prometheus_client.openmetrics.exposition import generate_latest as om_latest
        return om_latest(self._registry)
