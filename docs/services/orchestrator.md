# orchestrator-service

The orchestrator-service runs the ETL pipeline. It receives webhook events from the gateway via gRPC, fetches the changed file from Git, parses and validates it, then writes the config values to every configured sink.

---

## What it does

```
gateway (gRPC) → Celery task → fetch Git → parse → flatten → validate CUE → write sinks
```

Three stages:

1. **Payload** — resolve rule, platform, and target environment
2. **ETL** — fetch from Git, parse, flatten to `key: value` map, validate against CUE schema
3. **Sync** — write to sinks, prune stale keys, rollback on failure

---

## Running

```bash
cd orchestrator-service
pip install -e .

# API + gRPC server
uvicorn main:app --host 0.0.0.0 --port 8000

# Celery worker (separate terminal)
celery -A app.worker.celery worker --queues=webhooks,sync -l info
```

### With Docker

```bash
# API server
docker run \
  -p 8000:8000 \
  -p 50051:50051 \
  -e CONFIG_PATH=/config.cue \
  -v ./config.cue:/config.cue:ro \
  jarin-devoss/orchestrator-service:latest

# Celery worker
docker run \
  -e CONFIG_PATH=/config.cue \
  -v ./config.cue:/config.cue:ro \
  jarin-devoss/orchestrator-service:latest \
  celery -A app.worker.celery worker --queues=webhooks,sync -l info
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` enforces mTLS |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker |
| `CONFIG_PATH` | `config.cue` | CUE bundle path |
| `GIT_CACHE_DIR` | `/tmp/vt_gitcache` | Git repo LRU cache directory |
| `GIT_CACHE_MAX_ENTRIES` | `20` | Max cached repo checkouts |
| `SCHEMA_CACHE_DIR` | `/tmp/schema_registry` | CUE schema clone directory |
| `ORCHESTRATOR_ADDR` | `:50051` | gRPC listen address |

---

## API

### HTTP (port 8000)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/webhooks/{datasource}` | Ingest webhook, enqueue ETL task |
| `POST` | `/v1/webhooks/{datasource}/dry-run` | Simulate without writing |
| `GET` | `/v1/tasks/{task_id}` | Poll task status |
| `GET` | `/v1/health` | Liveness probe |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/docs` | OpenAPI / Swagger UI |

### gRPC (port 50051)

| RPC | Called by | Description |
|---|---|---|
| `ProcessWebhook` | gateway-service | Enqueue ETL pipeline |
| `TriggerSync` | watcher-service | Re-sync datasource on drift |
