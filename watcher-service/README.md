# watcher-service

The **watcher-service** keeps your datasources in step with Git. It holds no sink connections and no rules: on every poll it asks the orchestrator which rules to watch, asks whether each datasource still matches Git, and has the orchestrator restore the ones that drifted when `self_heal: true` is set on the rule.

```
                 ┌────────────────────────┐   ListWatchTargets   ┌──────────────────────┐
                 │    watcher-service     │ ───────────────────► │ orchestrator-service │
                 │                        │   CheckDatasource    │                      │
                 │  Manager: one loop per │ ───────────────────► │  reads the sinks,    │
                 │  rule, rules re-read   │   SyncDatasource     │  compares with the   │
                 │  every poll            │ ───────────────────► │  sync ledger, heals  │
                 │  admin :9091           │                      └──────────────────────┘
                 └────────────────────────┘
```

---

## What drift detection looks like

Say your config in Git specifies `max_connections: 50`. Someone edits MongoDB directly and changes it to `5`. On the next poll cycle:

1. The watcher asks the orchestrator whether the datasource still matches Git (`CheckDatasource`)
2. The orchestrator reads `max_connections` back from MongoDB and compares it with what it last synced from Git — mismatch detected
3. If `self_heal: true` on the rule → the watcher calls `SyncDatasource` on the orchestrator
4. The orchestrator re-runs the ETL pipeline and writes `max_connections: 50` back
5. `vartrack_watcher_drift_detected_total` counter increments, alertable via Prometheus

If `self_heal: false`, drift is logged and counted but no automatic repair is triggered.

---

## Features

- **Every sink the orchestrator writes** — the comparison runs in the orchestrator, key by key, so the watcher needs no sink credentials
- **Rules followed without a restart** — the list of rules to watch is re-read on every poll; `suspend` pauses a rule and `drift_ignore` lists keys the application changes itself
- **Configurable poll interval** — set per-service via `POLL_INTERVAL` (default 60s)
- **Self-heal on demand** — `self_heal: true` in the CUE rule triggers automatic repair via gRPC
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

## Which rules it watches

The watcher reads no rules. On every poll it asks the orchestrator which rules to watch (`Watcher.ListWatchTargets`): those with `self_heal: true` that are not suspended. The orchestrator is the one service that layers `config.cue`, a tenant's `bundle.json` and a repository's `vartrack.json` into a rule, so a rule that gains `self_heal`, is suspended or is removed reaches the watcher at the next poll, without a restart. If the orchestrator cannot be asked, the watcher keeps watching the rules it has.

```cue
rules: [
  {
    platform:   "github"
    datasource: "mongo"
    self_heal:  true   // ← the orchestrator lists it; the watcher auto-heals on drift
  },
  {
    platform:   "github"
    datasource: "zookeeper"
    self_heal:  false  // ← not listed (push-only)
  }
]
```

## The bundle (`CONFIG_PATH`)

`config.cue` is read for one setting: the datasource used for leader election (below). The rest of it is the gateway's and the orchestrator's.

---

## Leader election

When running multiple watcher replicas, leader election ensures only one runs the heal loop at a time:

```cue
bundle: {
  watcher_leader_election_datasource: "redis"  // or "zookeeper"; a datasource of the bundle
}
```

The Redis password may be written inline or as a `{file: …}`. A `{ref: …}` is refused at startup: the watcher has no secret manager.

**ZooKeeper** — ephemeral sequential znodes with predecessor-watch pattern. Each candidate watches only its immediate predecessor, avoiding thundering-herd wakeups.

**Redis** — `SET NX PX` distributed lock with Lua atomic renewal every 5 s (TTL: 15 s). If the leader process dies, another replica acquires the lock within one TTL window.

---

## Metrics

```
GET /metrics    # Prometheus (port 9091)
```

| Metric | Type | Description |
|---|---|---|
| `vartrack_watcher_drift_detected_total` | Counter | Drift events detected, labeled by datasource |
| `vartrack_watcher_heal_triggered_total` | Counter | Self-heal requests sent to the orchestrator |
| `vartrack_watcher_heal_failed_total` | Counter | Self-heal requests that failed |
| `vartrack_watcher_poll_duration_seconds` | Histogram | Time spent on a datasource's poll, labeled by datasource |
| `vartrack_watcher_up` | Gauge | Whether the watcher loop of a datasource is running |
| `vartrack_watcher_build_info` | Gauge | Version and commit of the build |

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
# From the repository root: the watcher builds against the shared libs/go
docker build -f watcher-service/Dockerfile -t vartrack/watcher-service:latest .

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
    manager.go           # Asks the orchestrator for the rules to watch; starts and stops a loop per rule
    watcher.go           # One rule's poll loop: CheckDatasource, then SyncDatasource on drift

  election/
    elector.go           # ZooKeeper leader election (ephemeral sequential znodes)
    redis_elector.go     # Redis leader election (SET NX PX + Lua renewal)

  healer/
    healer.go            # Calls the orchestrator: ListWatchTargets, CheckDatasource, SyncDatasource
    circuit_breaker.go   # Backs off when orchestrator is degraded

  monitoring/
    otel.go              # OpenTelemetry init + shutdown
    elk.go               # Elasticsearch log shipper
    metrics.go           # Prometheus metric definitions

  (the generated protobuf / gRPC stubs are the shared module ../libs/go/gen)
```
