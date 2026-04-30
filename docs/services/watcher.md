# watcher-service

The watcher-service continuously polls your configured datasources and compares the live state against the Git baseline. When drift is detected and `self_heal: true` is set, it calls the orchestrator to restore correct state automatically.

---

## What it does

```
Every POLL_INTERVAL
  ├── Read live values from each datasource
  ├── Compare against Git baseline
  ├── No diff   → OK
  └── Drift     → log + increment metric + (self_heal? → TriggerSync)
```

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
| `CONFIG_PATH` | `config.cue` | CUE bundle path |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | gRPC address of the orchestrator |
| `POLL_INTERVAL` | `60s` | How often each sink is polled |
| `HEAL_TIMEOUT` | `30s` | Max time for a self-heal call |
| `ADMIN_ADDR` | `:9091` | Health + metrics listener |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing |
| `ELK_ENABLED` | `false` | Enable Elasticsearch log shipping |

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
| `vartrack_watcher_drift_total` | Drift events by datasource |
| `vartrack_watcher_heal_total` | Self-heal calls triggered |
| `vartrack_watcher_poll_duration_seconds` | Poll cycle duration |
