"""
app/monitoring/outcomes.py
───────────────────────────
Typed outcome label constants — mirrors gateway's outcomes.go.

Using string constants keeps metric cardinality bounded and makes dashboards
and alert rules consistent across the codebase.
"""
from __future__ import annotations


# ── Webhook dispatch outcomes (orch_webhooks_total{outcome=...}) ──────────────

OUTCOME_ACCEPTED         = "accepted"          # task enqueued successfully
OUTCOME_NO_RULE          = "no_rule"           # no rule_config for datasource
OUTCOME_MISSING_REF      = "missing_ref"       # no repo_url or ref in payload
OUTCOME_ENQUEUE_ERROR    = "enqueue_error"     # Celery broker unreachable
OUTCOME_DRY_RUN          = "dry_run"           # dry-run — no write dispatched

# ── Celery task statuses (orch_tasks_total{status=...}) ──────────────────────

TASK_STATUS_SUCCESS      = "success"
TASK_STATUS_FAILURE      = "failure"
TASK_STATUS_RETRY        = "retry"
TASK_STATUS_DLQ          = "dlq"               # routed to dead-letter queue

# ── ETL file outcomes (orch_etl_files_total{outcome=...}) ─────────────────────

ETL_OUTCOME_OK               = "ok"
ETL_OUTCOME_EMPTY            = "empty"
ETL_OUTCOME_SKIPPED          = "skipped"
ETL_OUTCOME_ERROR            = "error"
ETL_OUTCOME_VALIDATION_FAILED= "validation_failed"
ETL_OUTCOME_ABORTED          = "aborted"

# ── Schema registry operations ────────────────────────────────────────────────

SCHEMA_OP_GET         = "get"
SCHEMA_OP_CLONE       = "clone"
SCHEMA_OP_INVALIDATE  = "invalidate"

SCHEMA_OUTCOME_HIT    = "hit"
SCHEMA_OUTCOME_MISS   = "miss"
SCHEMA_OUTCOME_ERROR  = "error"

# ── Git fetch outcomes ────────────────────────────────────────────────────────

GIT_OUTCOME_OK        = "ok"
GIT_OUTCOME_ERROR     = "error"
GIT_OUTCOME_CACHE_HIT = "cache_hit"

# ── gRPC status ───────────────────────────────────────────────────────────────

GRPC_STATUS_OK        = "ok"
GRPC_STATUS_ERROR     = "error"
