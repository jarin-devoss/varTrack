# watcher-service

The **watcher-service** continuously polls your configured datasources and compares their live state against what Git says they should contain. When drift is detected and `self_heal: true` is set on the rule, it calls the orchestrator to restore the correct state automatically.

```
                        ┌────────────────────────────────────────┐
                        │           watcher-service               │
                        │                                        │
  MongoDB  ─── poll ──► │  WatcherManager                       │──► TriggerSync (gRPC)
  ZooKeeper ── poll ──► │    ├── MongoWatcher                   │──► orchestrator-service
  Redis ──────  poll ──► │    ├── ZooKeeperWatcher              │
  S3 ──────── poll ──► │    ├── RedisWatcher                   │
  Linux server  poll ──► │    ├── S3Watcher                     │
  ConfigMap ─── poll ──► │    └── LinuxServerWatcher            │
  Helm ──────── poll ──► │                                        │
  Vercel ─────── poll ──► │  admin HTTP :9091  /health  /metrics  │
                        └────────────────────────────────────────┘
```

---

## What drift detection looks like

Say your config in Git specifies `max_connections: 50`. Someone edits MongoDB directly and changes it to `5`. On the next poll cycle:

1. The watcher reads the live `max_connections` value from MongoDB
2. It compares against the Git-sourced baseline — mismatch detected
3. If `self_heal: true` on the rule → calls `TriggerSync` on the orchestrator
4. The orchestrator re-runs the ETL pipeline and writes `max_connections: 50` back
5. `vartrack_watcher_drift_total` counter increments, alertable via Prometheus

If `self_heal: false`, drift is logged and counted but no automatic repair is triggered.

---

## Features

- **8 sink types** — MongoDB, ZooKeeper, Redis, S3, Linux server (SSH), Kubernetes ConfigMap, Helm, Vercel
- **Per-key diff** — detects added, changed, and deleted keys individually; flags on any mismatch
- **Configurable poll interval** — set per-service via `POLL_INTERVAL` (default 60s)
- **Self-heal on demand** — `self_heal: true` in the CUE rule triggers automatic repair via gRPC
- **Shared Redis state store** — multiple watcher replicas can share baseline state via Redis
- **Leader election** — ZooKeeper or Redis distributed lock; only one replica runs the heal loop
- **OpenTelemetry tracing** — distributed traces sent to Jaeger / otel-collector
- **Prometheus metrics** — poll count, drift count, heal count, poll duration — all labeled by datasource

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | Runtime environment (`production`, `staging`, `development`) |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `ADMIN_ADDR` | `:9091` | Listen address for admin HTTP (health + metrics) |
| `CONFIG_PATH` | `config.cue` | Path to the CUE bundle |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | gRPC address of the orchestrator-service |
| `POLL_INTERVAL` | `60s` | How often each watcher polls its sink |
| `HEAL_TIMEOUT` | `30s` | Max time allowed for a single self-heal call |
| `STATE_DIR` | `/tmp/watcher-state` | Local directory for baseline state snapshots |
| `TLS_CA_FILE` | — | CA cert to verify the orchestrator's TLS certificate |
| `TLS_CERT_FILE` | — | Client cert for mTLS |
| `TLS_KEY_FILE` | — | Client key for mTLS |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing |
| `OTEL_ENDPOINT` | `otel-collector:4317` | OTLP gRPC endpoint |
| `ELK_ENABLED` | `false` | Enable Elasticsearch log shipping |
| `ES_ENDPOINTS` | `http://elasticsearch:9200` | Elasticsearch cluster URL(s) |
| `ES_INDEX` | `vartrack-logs` | Elasticsearch index name |

> In `APP_ENV=production` the service refuses to start without `TLS_CA_FILE`, `TLS_CERT_FILE`, and `TLS_KEY_FILE`.

---

## CUE Bundle (`CONFIG_PATH`)

Only datasources whose rule has `self_heal: true` are actively watched. Others are skipped.

```cue
bundle: {
  platforms: [{
    github: {
      endpoint: "https://github.com"
      token:    {value: "ghp_xxxx"}
      org_name: "my-org"
    }
  }]

  datasources: [
    {
      mongo: {
        endpoint:   "mongodb://mongo:27017"
        database:   "vartrack"
        collection: "app_vars"
      }
    },
    {
      zookeeper: {
        hosts: ["zookeeper:2181"]
      }
    }
  ]

  rules: [
    {
      platform:   "github"
      datasource: "mongo"
      self_heal:  true   // ← watcher watches this and auto-heals on drift
    },
    {
      platform:   "github"
      datasource: "zookeeper"
      self_heal:  false  // ← not watched (push-only)
    }
  ]

  global_tags: {
    watcher_state_redis: "redis"  // which redis datasource stores watcher baseline state
  }
}
```

---

## Leader election

When running multiple watcher replicas, leader election ensures only one runs the heal loop at a time:

```cue
global_tags: {
  watcher_leader_election_datasource: "redis"  // or "zookeeper"
}
```

**ZooKeeper** — ephemeral sequential znodes with predecessor-watch pattern. Each candidate watches only its immediate predecessor, avoiding thundering-herd wakeups.

**Redis** — `SET NX PX` distributed lock with Lua atomic renewal every 5 s (TTL: 15 s). If the leader process dies, another replica acquires the lock within one TTL window.

---

## Metrics

```
GET /metrics    # Prometheus (port 9091)
```

| Metric | Type | Description |
|---|---|---|
| `vartrack_watcher_poll_total` | Counter | Total poll cycles, labeled by datasource |
| `vartrack_watcher_drift_total` | Counter | Drift events detected, labeled by datasource |
| `vartrack_watcher_heal_total` | Counter | Self-heal calls triggered |
| `vartrack_watcher_heal_errors_total` | Counter | Self-heal calls that failed |
| `vartrack_watcher_poll_duration_seconds` | Histogram | Poll cycle duration, labeled by datasource |

---

## Health probes

```
GET /health/liveness    # Always 200 while the process is alive
GET /health/readiness   # 200 when the orchestrator gRPC connection is ready
```

---

## Running locally

```bash
# From the watcher-service directory
APP_ENV=development \
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=../config.cue \
POLL_INTERVAL=10s \
go run ./cmd/...
```

---

## Testing

```bash
# Unit tests — no external services required
go test ./internal/...

# All tests
go test ./... -timeout 120s
```

---

## Docker

```bash
docker build -t vartrack/watcher-service:latest .

docker run \
  -p 9091:9091 \
  -v ./config.cue:/app/config.cue:ro \
  -e APP_ENV=production \
  -e ORCHESTRATOR_ADDR=orchestrator:50051 \
  -e CONFIG_PATH=/app/config.cue \
  -e POLL_INTERVAL=60s \
  vartrack/watcher-service:latest
```

---

## Architecture

```
cmd/
  main.go                # Startup, graceful shutdown, admin server

internal/
  config/
    env.go               # Environment variable loading
    bundle.go            # CUE bundle parsing (reads rules + datasources)

  watcher/
    manager.go           # Starts/stops all sink watchers concurrently
    watcher.go           # Watcher interface + poll loop
    mongo.go             # MongoDB drift detection
    zookeeper.go         # ZooKeeper drift detection
    redis.go             # Redis drift detection
    s3.go                # S3 drift detection
    linux_server.go      # SSH / Linux file drift detection
    configmap.go         # Kubernetes ConfigMap drift detection
    helm.go              # Helm release drift detection
    vercel.go            # Vercel edge config drift detection
    redis_state_store.go # Shared Redis state store (multi-replica)

  election/
    elector.go           # ZooKeeper leader election (ephemeral sequential znodes)
    redis_elector.go     # Redis leader election (SET NX PX + Lua renewal)

  healer/
    healer.go            # Calls orchestrator TriggerSync on drift
    circuit_breaker.go   # Backs off when orchestrator is degraded

  monitoring/
    otel.go              # OpenTelemetry init + shutdown
    elk.go               # Elasticsearch log shipper
    metrics.go           # Prometheus metric definitions

  gen/proto/go/          # Generated protobuf / gRPC stubs
```
