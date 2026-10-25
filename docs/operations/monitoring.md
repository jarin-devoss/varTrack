# Monitoring

Each service has an admin port, separate from the traffic it serves, with its health checks
and Prometheus metrics. Traces go to OpenTelemetry, and logs are JSON lines that can also be
shipped to Elasticsearch.

---

## Health checks

| Service | Port (default) | Liveness | Readiness | Metrics |
|---|---|---|---|---|
| gateway | `ADMIN_ADDR` (`:9090`) | `GET /health/liveness` | `GET /health/readiness` — the orchestrator connection is ready | `GET /metrics` |
| orchestrator | `ADMIN_PORT` (`9090`) | `GET /health/liveness` | `GET /health/readiness` | `GET /metrics` |
| watcher | `ADMIN_ADDR` (`:9091`) | `GET /health/liveness` | `GET /health/readiness` | `GET /metrics` |

`/healthz` is an alias of readiness on the gateway and of liveness on the orchestrator and
the watcher. The gateway's admin port also serves `/debug/pprof/` and, with `auth.opa`,
`POST /admin/opa/reload`; keep it off the public network.

The orchestrator also starts a Prometheus scrape server of its own when `PROM_ENABLED=true`,
on `PROM_PORT` (`9090`) at `PROM_METRICS_PATH` (`/metrics`). When both are on one host, give
them different ports — the e2e stack uses `ADMIN_PORT=9093` and `PROM_PORT=9090`.

---

## Metrics

### gateway (`gw_`)

| Metric | Type | Labels | What |
|---|---|---|---|
| `gw_webhooks_total` | counter | `datasource`, `platform`, `event_type`, `outcome` | Webhooks received, by result |
| `gw_webhook_processing_seconds` | histogram | `datasource`, `platform`, `event_type` | Receipt to the orchestrator's acknowledgement |
| `gw_webhook_payload_bytes` | histogram | `platform` | Webhook body size |
| `gw_orchestrator_requests_total` | counter | `rpc_method`, `status` | gRPC calls to the orchestrator |
| `gw_orchestrator_request_duration_seconds` | histogram | `rpc_method` | Their latency |
| `gw_orchestrator_connection_state` | gauge | `state` | 1 for the gRPC connection's current state |
| `gw_circuit_breaker_state` | gauge | — | 0 closed, 1 open, 2 half-open |
| `gw_circuit_breaker_state_changes_total` | counter | `from_state`, `to_state` | Breaker transitions |
| `gw_circuit_breaker_open_rejections_total` | counter | — | Requests refused while open |
| `gw_rate_limit_hits_total` | counter | `limiter_type`, `scope` | Requests refused by the rate limiter |
| `gw_secret_resolutions_total` | counter | `manager`, `outcome` | Secret lookups (`hit`, `miss`, `error`) |
| `gw_secret_cache_size` | gauge | — | Secrets held in the cache |
| `gw_http_requests_total`, `gw_http_request_duration_seconds`, `gw_http_request_body_bytes`, `gw_http_active_requests` | | `method`, `path`, … | Every HTTP request |
| `gw_build_info` | gauge | `version`, `commit`, `go_version` | Always 1 |

### orchestrator (`orch_`)

| Metric | Type | Labels | What |
|---|---|---|---|
| `orch_webhooks_total` | counter | `datasource`, `platform`, `outcome` | Webhooks queued for a worker |
| `orch_webhook_dispatch_duration_seconds` | histogram | `datasource`, `platform` | gRPC receipt to the task being queued |
| `orch_tasks_total` | counter | `task_name`, `queue`, `status` | Task runs |
| `orch_task_duration_seconds` | histogram | `task_name`, `queue` | Task run time |
| `orch_task_retries_total` | counter | `task_name` | Task retries |
| `orch_task_queue_depth` | gauge | `queue` | Messages waiting per queue |
| `orch_etl_stage_duration_seconds` | histogram | `stage` | Each pipeline stage |
| `orch_etl_files_total` | counter | `outcome` | Files processed |
| `orch_etl_keys_total` | counter | — | Flat keys read |
| `orch_mongo_write_duration_seconds` | histogram | `sync_mode` | Write time per file — for **every** sink, despite the name |
| `orch_mongo_documents_written_total` | counter | `datasource`, `sync_mode` | Keys written — every sink |
| `orch_mongo_documents_pruned_total` | counter | `datasource` | Keys pruned — every sink |
| `orch_mongo_errors_total` | counter | `operation` | Write and rollback errors — every sink |
| `orch_git_fetch_duration_seconds` | histogram | `outcome` | Reading a file from Git |
| `orch_schema_operations_total` | counter | `operation`, `outcome` | Schema repository operations |
| `orch_schema_clone_duration_seconds` | histogram | `tenant_id` | Schema repository clones and refreshes |
| `orch_grpc_requests_total`, `orch_grpc_request_duration_seconds` | | `rpc_method`, … | Inbound gRPC |
| `orch_http_requests_total`, `orch_http_request_duration_seconds`, `orch_http_request_body_bytes`, `orch_http_active_requests` | | `method`, `path`, … | Inbound HTTP |
| `orch_build_info` | gauge | `version`, `commit`, `python_version` | Always 1 |

### watcher (`vartrack_watcher_`)

| Metric | Type | Labels | What |
|---|---|---|---|
| `vartrack_watcher_up` | gauge | `datasource` | 1 while a rule's poll loop runs |
| `vartrack_watcher_build_info` | gauge | `version`, `commit`, `go_version` | Always 1 |

`vartrack_watcher_drift_detected_total`, `vartrack_watcher_heal_triggered_total`,
`vartrack_watcher_heal_failed_total` and `vartrack_watcher_poll_duration_seconds` are
registered but not recorded by the current watcher: they stay at zero. Until they are, follow
drift with the web UI's **Environments** page and the `DriftDetected`, `HealSucceeded` and
`HealFailed` events.

---

## Alerts worth having

| Alert | Expression (sketch) |
|---|---|
| Webhooks refused | `sum(rate(gw_webhooks_total{outcome!="success"}[15m])) > 0` — check the `outcome` values your gateway reports |
| Orchestrator unreachable from the gateway | `gw_circuit_breaker_state == 1` |
| Tasks failing | `sum(rate(orch_tasks_total{status="failure"}[15m])) > 0` |
| Queue backing up | `orch_task_queue_depth > 100` for 10 minutes |
| A rule's watcher stopped | `vartrack_watcher_up == 0` |
| Service down | the readiness check failing |

The label values (`outcome`, `status`) are the ones the services emit; look at a scrape
(`curl :9090/metrics`) before writing an alert on them.

---

## Traces and logs

| | gateway | orchestrator | watcher |
|---|---|---|---|
| OpenTelemetry | `OTEL_ENABLED`, `OTEL_ENDPOINT` (`otel-collector:4317`), `OTEL_SERVICE_NAME` | `OTEL_ENABLED`, `JAEGER_ENABLED`, `JAEGER_ENDPOINT` | `OTEL_ENABLED` |
| Elasticsearch | `ELK_ENABLED`, `ES_ENDPOINTS`, `ES_INDEX` | `ELK_ENABLED` | `ELK_ENABLED` |
| Log level | `LOG_LEVEL` | `LOG_LEVEL` | `LOG_LEVEL` |

All variables are in [Environment variables](../reference/environment-variables.md).
