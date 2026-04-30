# gateway-service

The gateway-service is the HTTP ingress layer of varTrack. It receives webhooks from Git platforms, routes them to the correct datasource rule, and forwards them to the orchestrator-service via gRPC.

---

## What it does

```
GitHub webhook  ──HTTPS──►  gateway-service  ──gRPC──►  orchestrator-service
                             :5657                         :50051
                                │
                             :9090  metrics + health
```

1. Receives a `push` or `pull_request` event
2. Matches the request to a datasource rule in the bundle
3. Forwards the payload to the orchestrator via gRPC
4. Returns `202 Accepted` — processing is async

---

## Running

```bash
APP_ENV=dev \
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=config.cue \
go run ./gateway-service/cmd
```

### With Docker

```bash
docker run \
  -p 5657:5657 \
  -p 9090:9090 \
  -v ./config.cue:/config.cue:ro \
  -e APP_ENV=production \
  -e ORCHESTRATOR_ADDR=orchestrator:50051 \
  -e CONFIG_PATH=/config.cue \
  jarin-devoss/gateway-service:latest
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `test` | `production` enables TLS and signature enforcement |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `GATEWAY_ADDR` | `:5657` | Webhook listener |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | gRPC address of the orchestrator |
| `CONFIG_PATH` | `config.cue` | CUE bundle path |
| `ADMIN_ADDR` | `:9090` | Metrics + health listener |
| `GRPC_TLS_CERT` | — | Client cert for mTLS outbound gRPC |
| `GRPC_TLS_KEY` | — | Client key for mTLS outbound gRPC |
| `GATEWAY_TLS_CERT` | — | Inbound TLS cert |
| `GATEWAY_TLS_KEY` | — | Inbound TLS key |

---

## API

### Webhook endpoint

```
POST /webhooks/{datasource}
```

| Status | Description |
|---|---|
| `202 Accepted` | Webhook forwarded to orchestrator |
| `200 OK` | Event type not handled (e.g. `ping`) |
| `400` | Bad request (missing header, invalid JSON, validation error) |
| `401` | Signature invalid or replay detected |
| `404` | No rule configured for this datasource |
| `413` | Request body exceeds `max_webhook_body_bytes` |
| `429` | Rate limit exceeded |
| `502` | gRPC call to orchestrator failed |
| `503` | Circuit breaker open |

### Health

```
GET /health/liveness     # 200 while alive
GET /health/readiness    # 200 when orchestrator connection is ready
GET /metrics             # Prometheus metrics (port 9090)
```
