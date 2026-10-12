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
| `UI_DEFAULT_TENANT` | first registered | Tenant the web UI reads when the request has no `X-Tenant-ID` |
| `UI_COMPARE_TTL_SECONDS` | `10` | How long a sink comparison is answered as is before it is refreshed in the background |

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

### Web UI read API (port 8000)

The web UI reads everything it shows from these endpoints. The tenant comes from the `X-Tenant-ID` header, then `UI_DEFAULT_TENANT`, then the first registered tenant.

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/ui/rules` | Every rule of the tenant with one target per synced environment |
| `GET` | `/v1/ui/rules/{platform}/{datasource}` | One rule |
| `GET` | `/v1/ui/rules/{platform}/{datasource}/targets/{env}/keys` | Desired value (Git) and current value (sink) of every key |
| `POST` | `/v1/ui/rules/{platform}/{datasource}/sync` | Re-sync environments from the branch head in Git — body `{"envs": [], "dryRun": false, "prune": false}` |
| `GET` | `/v1/ui/events?rule=&limit=` | Pushes, syncs, drift and heals, newest first |
| `GET` | `/v1/ui/tasks?rule=&limit=` | Sync tasks, newest first |
| `GET` | `/v1/ui/tasks/{task_id}` | One task with its per-file results |
| `GET` | `/v1/ui/bundle` | Platforms and datasources, without credentials |

Every sync records the desired state it wrote (secret values only as a digest), its task and its events in Redis next to the Celery broker. Reading a rule compares that desired state with the values read back from the sink, so drift shows key by key even for rules without `self_heal`.

Pages never wait on a sink they have read before. Each comparison is cached: for `UI_COMPARE_TTL_SECONDS` (default 10) it is answered as is, and after that it is still answered at once while the sink is read again in the background — every target shows when it was last checked. The API reads every sink once at startup, and the schema registry's periodic `git fetch` also runs behind requests. Only an environment not read since its last sync is read while the request waits.

### gRPC (port 50051)

| RPC | Called by | Description |
|---|---|---|
| `ProcessWebhook` | gateway-service | Enqueue ETL pipeline |
| `CheckDatasource` | watcher-service | Does the datasource still match Git? `in_sync`, `drifted`, `syncing` or `unknown`, per environment |
| `SyncDatasource` | watcher-service | Re-sync one datasource from Git on drift (self-heal) |
