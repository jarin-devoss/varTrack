# orchestrator-service

The **orchestrator-service** is the ETL engine of VarTrack. It receives webhook events from the gateway via gRPC, fetches the changed file from Git, parses and validates it, then writes the config values to every configured sink.

```
gateway-service (gRPC) ──► Celery task queue ──► ETL pipeline ──► MongoDB
                            (Redis broker)                         Redis
                                                                   S3
                                                                   ZooKeeper
                                                                   ConfigMap
                                                                   Helm
                                                                   Vercel
                                                                   Linux server
```

---

## What happens on a push

```
1. Webhook arrives
   → resolve platform + rule → PayloadContext

2. ETL stage
   → clone / fetch the pushed Git ref (LRU cache, max 20 repos)
   → parse file  (YAML / JSON / TOML / .env / HCL / XML / INI)
   → slice subtree under "vartrack" root key
   → detect environment key pattern (branch-keyed or flat)
   → flatten to { "key.subkey": "value" }  ← Rust BFS/DFS
   → apply variables_map overlay
   → validate against CUE schema (per-tenant, fetched from schema repo)

3. Sync stage
   → pick strategy: LIVE_STATE / GIT_UPSERT_ALL / GIT_SMART_REPAIR
   → write to configured sinks in parallel
   → prune stale keys if configured
   → rollback on failure (delete written data)
```

---

## Supported formats

| Extension | Format |
|---|---|
| `.yaml`, `.yml` | YAML |
| `.json` | JSON |
| `.toml` | TOML |
| `.env` | dotenv / KEY=VALUE |
| `.ini` | INI |
| `.hcl` | HCL (Terraform-style) |
| `.xml` | XML |

Format is auto-detected from the file extension. Use the `--format` flag on `vt sync` to override.

---

## Rust core (`rust_core/`)

Hot-path operations are implemented in Rust and exposed to Python via PyO3. Pure-Python fallbacks activate automatically when the Rust extension is not compiled.

| Module | Purpose |
|---|---|
| `flatten` | Non-recursive BFS / DFS dict flattening |
| `merge` | Variable map overlay + environment resolution |
| `prune` | Stale-key detection and deferred deletion |
| `sync` | BLAKE3 content hash for `GIT_SMART_REPAIR` |
| `cue` | CUE binary subprocess wrapper |

Build the Rust extension:

```bash
cd rust_core
maturin develop --release
# or: make rust
```

---

## Sync strategies

| Mode | When used | Description |
|---|---|---|
| `GIT_UPSERT_ALL` | ≤ 500 keys (default) | Bulk-upsert every key from the payload |
| `GIT_SMART_REPAIR` | > 500 keys or explicit | Read live state, diff, write only changed keys |
| `LIVE_STATE` | Low-latency sinks | Direct overwrite without a read |
| `AUTO` | Default | Size-based heuristic selects mode at runtime |

Override per rule in `config.cue`:
```cue
sync_mode: "SYNC_MODE_GIT_SMART_REPAIR"
```

---

## `destination_template`

Controls the storage path, collection name, or key prefix in every sink. Placeholders `{tenant}` and `{env}` are resolved at write time.

| Sink | Controls | Template → Resolved |
|---|---|---|
| `mongo` | Collection name | `"{env}-config"` → `production-config` |
| `zookeeper` | znode root path | `"/{tenant}/{env}"` → `/acme/pr-42` |
| `redis` | Key prefix | `"{env}:cfg"` → `production:cfg` |
| `s3` | S3 key prefix | `"{tenant}/{env}/"` → `acme/production/` |
| `linux_server` | Full remote file path | `"/etc/app/{env}.env"` → `/etc/app/production.env` |
| `helm` | Helm release name | `"app-{env}"` → `app-production` |
| `configmap` | ConfigMap name | `"myapp-{env}"` → `myapp-production` |

---

## Environment resolution

Priority order when resolving the environment from a webhook event:

1. `branch_map[branch]` — explicit mapping (`main` → `production`)
2. `file_path_map[file_path]` — glob patterns on the changed file path
3. `env_as_pr: true` → `pr-{number}`
4. `env_as_branch: true` → branch name
5. `env_as_tags: true` → tag name
6. `"default"` fallback

---

## API

### HTTP (port 8000)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/webhooks/{datasource}` | Ingest webhook, enqueue ETL task |
| `POST` | `/v1/webhooks/{datasource}/dry-run` | Simulate ETL without writing anything |
| `POST` | `/v1/webhooks/schema-registry` | Re-clone schema repo on push |
| `GET` | `/v1/tasks/{task_id}` | Poll task status |
| `GET` | `/v1/health` | Liveness probe |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/docs` | OpenAPI / Swagger UI |

**Webhook response:**
```json
{ "task_id": "abc-123", "message": "accepted", "received_at": 1700000000.0 }
```

**Dry-run response:**
```json
{
  "task_id": "abc-456",
  "message": "dry_run_accepted",
  "dry_run": true,
  "report": { "files_processed": 1, "keys_written": 12, "keys_skipped": 0 }
}
```

### gRPC (port 50051)

| RPC | Called by | Description |
|---|---|---|
| `ProcessWebhook` | gateway-service | Enqueue the ETL pipeline for a webhook |
| `TriggerSync` | watcher-service | Re-sync a datasource on detected drift |

### Celery queues

| Queue | Task | Description |
|---|---|---|
| `webhooks` | `process_webhook_task` | Full ETL pipeline per webhook |
| `schema` | `refresh_schema_task` | Re-clone schema repo on push |
| `sync` | `sync_all_task` | Periodic full re-sync (every 5 min) |
| `dlq` | — | Dead-letter queue for permanently failed tasks |

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | Runtime environment (`production`, `staging`, `development`, `test`) |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker |
| `CONFIG_PATH` | `config.cue` | Path to the shared CUE bundle |
| `SCHEMA_CACHE_DIR` | `/tmp/schema_registry` | Root directory for per-tenant CUE schema clones |
| `SCHEMA_TTL_SECONDS` | `300` | How long a schema clone stays fresh (seconds) |
| `GIT_CACHE_DIR` | `/tmp/vt_gitcache` | Root directory for Git repo cache |
| `GIT_CACHE_MAX_ENTRIES` | `20` | LRU cap on cached repo checkouts |
| `CUE_BINARY` | `cue` | Path to the CUE binary |
| `CUE_TIMEOUT_SECONDS` | `15` | Subprocess timeout for `cue vet` |
| `ORCHESTRATOR_ADDR` | `:50051` | gRPC listen address |
| `MTLS_ENABLED` | `false` (non-prod) / `true` (prod) | Enable mTLS for inbound gRPC |
| `DEBUG` | `false` | Enable FastAPI debug mode |
| `CORS_ORIGINS` | `[]` | Allowed CORS origins |
| `APP_VERSION` | `dev` | Injected at build time |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Redis (Celery broker)
- MongoDB (primary sink)
- Rust + maturin (optional — Rust extension; falls back to pure Python)
- CUE binary (optional — schema validation; skipped if not installed)

### Install and run

```bash
cd orchestrator-service

# Install dependencies
pip install -e ".[test,dev]"   # local dev (includes linters + test deps)

# Build Rust extension (optional, for best performance)
make rust

# Start the API + gRPC server
make dev           # uvicorn --reload on :8000

# Start the Celery worker (separate terminal)
make worker        # queues: webhooks, sync

# Start Celery beat for periodic re-sync (separate terminal)
make beat

# Monitor tasks with Flower (separate terminal)
make flower        # http://localhost:5555
```

---

## Testing

```bash
make test           # all tests
make test-unit      # unit tests only (no infra required)
make test-int       # integration tests only
make test-cov       # with HTML coverage report
```

---

## Docker

```bash
# Build
docker build \
  --build-arg VERSION=1.0.0 \
  --build-arg COMMIT=$(git rev-parse --short HEAD) \
  -t vartrack/orchestrator-service:1.0.0 \
  -f docker/Dockerfile .

# Run API server
make docker-run

# Run Celery worker
make docker-worker
```

| Container role | Command |
|---|---|
| API + gRPC (default) | `python main.py` |
| Celery worker | `celery -A app.worker.celery worker --queues=webhooks,sync -l info` |
| Celery beat | `celery -A app.worker.celery beat -l info` |
| Flower | `celery -A app.worker.celery flower --port=5555` |

---

## Repository layout

```
orchestrator-service/
├── main.py                     # Entry point — starts uvicorn + gRPC daemon
├── pyproject.toml              # Build config, dependencies, pytest settings
├── Makefile                    # Dev workflow
├── docker/
│   └── Dockerfile              # Multi-stage build
│
├── app/
│   ├── config.py               # All settings (env vars + CUE bundle overrides)
│   ├── api/
│   │   ├── app.py              # FastAPI factory
│   │   └── routers/
│   │       ├── webhooks.py     # POST /v1/webhooks/{datasource}
│   │       ├── dry_run.py      # POST /v1/webhooks/{datasource}/dry-run
│   │       ├── tasks.py        # GET /v1/tasks/{task_id}
│   │       └── schemas.py      # Schema management
│   ├── grpc_server/            # ProcessWebhook + TriggerSync gRPC handlers
│   ├── pipeline/
│   │   ├── stage_etl.py        # Stage 2: Extract → Transform → Validate
│   │   ├── stage_sync.py       # Stage 3: Write + rollback
│   │   ├── transformer.py      # Flatten + variables_map overlay
│   │   ├── validator.py        # CUE schema validation subprocess
│   │   ├── git_extractor.py    # Git file fetch with LRU cache
│   │   ├── env_resolver.py     # Branch / PR / tag → env string
│   │   └── sinks/
│   │       ├── mongo.py        # MongoDB sink
│   │       ├── redis.py        # Redis sink
│   │       ├── s3.py           # S3 sink
│   │       ├── zookeeper.py    # ZooKeeper sink
│   │       ├── configmap.py    # Kubernetes ConfigMap sink
│   │       ├── helm.py         # Helm values sink
│   │       ├── vercel.py       # Vercel environment sink
│   │       ├── linux_server.py # SSH file sink
│   │       └── key_formatter.py# {tenant}/{env} placeholder resolution
│   ├── parsers/                # YAML / JSON / TOML / .env / HCL / XML / INI
│   ├── tasks/                  # Celery task definitions
│   └── schema_registry/        # Per-tenant CUE schema clone manager
│
├── rust_core/                  # Rust extension (PyO3 / maturin)
│   └── src/
│       ├── flatten.rs          # BFS / DFS flattening
│       ├── merge.rs            # Env resolution + overlay
│       ├── prune.rs            # Stale-key detection
│       ├── diff.rs             # Added / removed / changed sets
│       ├── sync.rs             # BLAKE3 content hash
│       └── cue.rs              # CUE subprocess wrapper
│
└── tests/
    ├── unit/                   # Per-module unit tests (no infra needed)
    └── integration/            # Full HTTP path end-to-end tests
```
