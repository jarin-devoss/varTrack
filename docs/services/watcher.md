# watcher-service

For every rule with `self_heal: true`, the watcher-service asks the orchestrator on each poll whether the datasource still matches Git (`CheckDatasource`), and has it restore the ones that drifted (`SyncDatasource`). The orchestrator reads the sinks and builds the rules; the watcher holds no sink credentials and reads no rule. It needs only the orchestrator's address, and `config.cue` for [leader election](../concepts/drift-detection.md#multi-replica-deployments).

---

## What it does

```
Every POLL_INTERVAL
  ├── ListWatchTargets (the orchestrator lists the rules to watch: self_heal on, not suspended)
  └── for each rule
        └── CheckDatasource (the orchestrator reads the sink and compares with Git, key by key)
              ├── in sync  → OK
              ├── syncing  → look again next poll
              ├── unknown  → no heal
              └── drifted  → log + increment metric + SyncDatasource (heal)
```

### Which rules it watches

The watcher does not read rules from `config.cue`, a tenant's `bundle.json` or a repository's `vartrack.json`. The orchestrator is the one service that layers those into the rule a sync runs with, so the watcher asks it which rules to watch, on every poll. A rule that gains `self_heal`, is suspended or is removed reaches the watcher at the next poll; nothing needs a restart.

- If the orchestrator cannot be asked, the watcher keeps watching the rules it has.
- If it cannot be asked at startup, the watcher asks again, waiting a little longer each time (up to one poll interval).
- A rule of two tenants is two targets: each tenant's is checked and healed on its own.

See [Drift Detection & Self-Heal](../concepts/drift-detection.md) for the edge cases (unknown, a heal underway, a sync that died).

---

## Running

```bash
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=config.cue \
POLL_INTERVAL=60s \
go run ./watcher-service/cmd
```

### With Docker

```bash
docker run \
  -p 9091:9091 \
  -v ./config.cue:/app/config.cue:ro \
  -e APP_ENV=production \
  -e ORCHESTRATOR_ADDR=orchestrator:50051 \
  -e CONFIG_PATH=/app/config.cue \
  -e POLL_INTERVAL=60s \
  jarin-devoss/watcher-service:latest
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` requires TLS credentials |
| `CONFIG_PATH` | `config.cue` | CUE bundle path; read only for `watcher_leader_election_datasource` |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | gRPC address of the orchestrator |
| `POLL_INTERVAL` | `60s` | How often the rules to watch are listed and each is checked |
| `HEAL_TIMEOUT` | `30s` | Max time for a self-heal call |
| `ADMIN_ADDR` | `:9091` | Health + metrics listener |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing |
| `ELK_ENABLED` | `false` | Enable Elasticsearch log shipping |

Every variable, with the observability ones, is in [Environment variables](../reference/environment-variables.md).

---

## Health + metrics

```
GET /health/liveness    # 200 while alive
GET /health/readiness   # 200 when orchestrator connection is ready
GET /metrics            # Prometheus metrics (port 9091)
```

Key metrics:

| Metric | Description |
|---|---|
| `vartrack_watcher_drift_detected_total` | Drift found, by rule |
| `vartrack_watcher_heal_triggered_total` | Heals the orchestrator accepted |
| `vartrack_watcher_heal_failed_total` | Heals that could not be requested |
| `vartrack_watcher_poll_duration_seconds` | How long a `CheckDatasource` took |
