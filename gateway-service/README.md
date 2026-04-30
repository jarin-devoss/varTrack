# gateway-service

The **gateway-service** is the HTTP ingress layer of the VarTrack platform. It receives SCM webhooks (GitHub, GitLab, Bitbucket) over HTTPS, routes them to the correct datasource rule, and forwards them to the **orchestrator-service** via gRPC.

```
Webhook source  ──HTTPS──►  gateway-service  ──gRPC──►  orchestrator-service
                             (port 5657)                    (port 50051)
                                  │
                             admin + metrics
                             (port 9090)
```

---

## What it does

- Receives `push` and `pull_request` events from GitHub (and compatible SCM platforms)
- Looks up the matching rule in the CUE bundle based on the `{datasource}` path segment
- Verifies the webhook signature and forwards the payload to the orchestrator via gRPC
- Returns `202 Accepted` immediately — processing is async

### Routing

Each webhook URL maps to a named datasource in your bundle:

```
POST /webhooks/mongo       → routes to rules with datasource: "mongo"
POST /webhooks/redis       → routes to rules with datasource: "redis"
POST /webhooks/zookeeper   → routes to rules with datasource: "zookeeper"
```

The gateway reads the `config.cue` bundle to know which platforms, datasources, and rules are active. No code changes needed to add a new datasource — just update the bundle.

---

## Configuration

All configuration is provided through environment variables. An optional `.env` file in the working directory is loaded first; real environment variables always override it.

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `test` | Runtime environment: `production` enables TLS and signature enforcement |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `GATEWAY_ADDR` | `:5657` | Listen address for the main HTTP server |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | gRPC address of the orchestrator-service |
| `CONFIG_PATH` | `config.cue` | Path to the CUE bundle file |
| `ADMIN_ADDR` | `:9090` | Listen address for metrics and health |
| `VAULT_SECRET` | — | Vault secret path (masked in logs) |
| `GRPC_TLS_CA` | — | CA cert for outbound gRPC TLS |
| `GRPC_TLS_CERT` | — | Client cert for mTLS |
| `GRPC_TLS_KEY` | — | Client key for mTLS |
| `GATEWAY_TLS_CERT` | — | Inbound TLS cert |
| `GATEWAY_TLS_KEY` | — | Inbound TLS key |
| `ENV_FILE` | — | Override path to the `.env` file |

> In `APP_ENV=production` the service refuses to start without TLS credentials, and webhook signature verification is mandatory.

### CUE Bundle (`CONFIG_PATH`)

The bundle declares platforms, datasources, and routing rules. Example:

```cue
bundle: {
  platforms: [{
    github: {
      endpoint:        "https://github.com"
      push_event_name: "push"
      pr_event_name:   "pull_request"
      secret:          {value: "my-webhook-secret"}
    }
  }]

  datasources: [{
    mongo: {
      endpoint: "mongodb://mongo:27017"
      database: "vartrack"
    }
  }]

  rules: [{
    platform:             "github"
    datasource:           "mongo"
    repositories:         ["my-org/*"]
    destination_template: "{env}-config"
    self_heal:            true
  }]
}
```

---

## API

### Webhook ingestion

```
POST /webhooks/{datasource}
```

Accepts a JSON webhook from a configured platform for the named datasource.

**Required headers:**
- `Content-Type: application/json`
- Platform event header (e.g. `X-GitHub-Event: push`)

**Responses:**

| Status | Code | Description |
|---|---|---|
| `202 Accepted` | — | Webhook accepted and forwarded |
| `200 OK` | — | Event type not handled (e.g. `ping`) — ignored |
| `400 Bad Request` | `GW_PLATFORM_MISMATCH` | Missing platform event header |
| `400 Bad Request` | `GW_INVALID_JSON` | Body is not valid JSON |
| `400 Bad Request` | `GW_PAYLOAD_VALIDATION` | Missing required fields |
| `401 Unauthorized` | `GW_SIGNATURE_INVALID` | HMAC signature mismatch |
| `401 Unauthorized` | `GW_REPLAY_DETECTED` | Duplicate delivery or stale timestamp |
| `404 Not Found` | `GW_DATASOURCE_NOT_FOUND` | No rule configured for this datasource |
| `429 Too Many Requests` | — | Rate limit exceeded |
| `502 Bad Gateway` | `GW_ORCHESTRATOR_ERROR` | gRPC call to orchestrator failed |
| `503 Service Unavailable` | `GW_ORCHESTRATOR_UNAVAILABLE` | Circuit breaker is open |

**Error response body:**
```json
{
  "code":    "GW_DATASOURCE_NOT_FOUND",
  "message": "no rule configured for datasource 'redis'",
  "status":  404
}
```

### Schema-registry webhook

```
POST /webhooks/schema-registry
```

Triggers a schema repo re-clone in the orchestrator. Only `push` events are forwarded.

### Health probes

```
GET /health/liveness    # Always 200 while the process is alive
GET /health/readiness   # Checks gRPC connection + Vault
```

### Metrics

```
GET /metrics    # Prometheus metrics (port 9090)
```

---

## Running locally

```bash
# From the gateway-service directory
APP_ENV=test \
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=../config.cue \
go run ./cmd/...
```

---

## Testing

```bash
# Unit tests — no external services needed
go test ./internal/...

# End-to-end tests — spins up a fake gRPC orchestrator in-process
go test ./e2e/...

# Everything
go test ./...
```

End-to-end scenarios covered: push events, ignored event types, unknown datasources, missing headers, invalid JSON, oversized bodies, replay attacks, signature verification, concurrent requests, and all health probe branches.

---

## Docker

```bash
# Build
docker build -t vartrack/gateway-service:latest .

# Run
docker run \
  -p 5657:5657 \
  -p 9090:9090 \
  -v ./config.cue:/config.cue:ro \
  -e APP_ENV=production \
  -e ORCHESTRATOR_ADDR=orchestrator:50051 \
  -e CONFIG_PATH=/config.cue \
  vartrack/gateway-service:latest
```

---

## Architecture

```
cmd/
  main.go               # Startup, signal handling, graceful shutdown
  tls_credentials.go    # Rotating mTLS certificate loader (30 s interval)

internal/
  server.go             # HTTP/HTTPS server
  router.go             # Route wiring, lazy platform initialisation
  admin.go              # Admin server (metrics, pprof)

  config/
    env.go              # Environment variable loading
    loader.go           # CUE bundle parsing

  handlers/
    webhooks.go         # Webhook ingestion and gRPC forwarding
    health.go           # Liveness + readiness probes
    errors.go           # Structured error codes (GW_ prefix)

  middlewares/
    request_id.go       # X-Request-ID generation
    correlation.go      # X-Correlation-ID propagation
    replay_protection.go# Nonce tracking + timestamp windows
    rate_limit.go       # Per-IP rate limiting
    keyed_rate_limit.go # Per IP:datasource rate limiting
    circuit_breaker.go  # Orchestrator fault isolation
    security_headers.go # Defensive HTTP headers
    recovery.go         # Panic recovery

  models/
    bundle.go           # Platform and datasource lazy init + cache
    platform.go         # Platform interface + registry
    datasource.go       # Datasource naming helpers

  gen/proto/go/         # Generated protobuf / gRPC code

e2e/
  e2e_test.go                  # Core end-to-end suite
  webhook_scenarios_test.go    # Extended scenarios
  signature_verification_test.go
```

---

## Security

Webhook signatures (HMAC-SHA256), replay protection, and mTLS are all enforced when `APP_ENV=production`. They are bypassed in `test`/`dev` mode to allow local development without a webhook secret. See [SECURITY.md](../SECURITY.md) for details.
