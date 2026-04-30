# Docker Compose Quickstart

The fastest way to run varTrack in production is with the provided `docker-compose.yml` at the repo root. It uses pre-built images from Docker Hub and starts the complete stack — gateway, orchestrator, worker, watcher, MongoDB, and Redis — with a single command.

---

## Prerequisites

- Docker 24+ and Docker Compose v2
- A `config.cue` bundle (see [Bundle Reference](../configuration/bundle.md))
- A GitHub (or GitLab / Gitea) repository with a webhook pointing at your server

---

## 1. Configure

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

The only required change is `MONGO_ROOT_PASSWORD`. Everything else has a sensible default:

```bash
# .env — minimum required
MONGO_ROOT_PASSWORD=your-strong-password
```

Full `.env` reference:

| Variable | Default | Description |
|---|---|---|
| `CONFIG_PATH` | `./config.cue` | Path to your CUE bundle |
| `APP_ENV` | `production` | `production` enables mTLS; `development` is permissive |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MONGO_ROOT_USER` | `vartrack` | MongoDB root username |
| `MONGO_ROOT_PASSWORD` | *(required)* | MongoDB root password |
| `GATEWAY_PORT` | `5657` | Host port for the webhook receiver |
| `ORCHESTRATOR_PORT` | `8000` | Host port for the orchestrator HTTP API |
| `WORKER_CONCURRENCY` | `4` | Celery worker concurrency |
| `WATCHER_POLL_INTERVAL` | `60s` | How often the watcher checks for drift |
| `CORS_ORIGINS` | *(empty)* | Browser origin for CORS headers (optional) |

---

## 2. Write your `config.cue`

Place `config.cue` next to `docker-compose.yml` (or set `CONFIG_PATH` to point elsewhere). Minimal example with GitHub → MongoDB:

```cue
bundle: {
  platforms: [{
    github: {
      endpoint:        "https://github.com"
      push_event_name: "push"
      pr_event_name:   "pull_request"
      secret:          "your-webhook-secret"   // HMAC secret set in GitHub
      token:           "ghp_xxx"               // PAT for cloning private repos
    }
  }]

  datasources: [{
    mongo: {
      endpoint:   "mongodb://vartrack:your-strong-password@mongo:27017"
      database:   "vartrack"
      collection: "variables"
    }
  }]

  rules: [{
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/app.yaml"
    repositories:         ["my-org/*"]
    destination_template: "{env}-config"
    self_heal:            true
    branch_map: {
      main:    "production"
      develop: "staging"
    }
  }]

  celery_broker_datasource:   "redis"
  celery_backend_datasource:  "redis"
  watcher_state_datasource:   "redis"

  datasources: [{
    redis: {
      host: "redis"
      port: 6379
      db:   0
    }
  }]
}
```

See the [Bundle Reference](../configuration/bundle.md) for all platforms, datasources, and rule options.

---

## 3. Start the stack

```bash
docker compose up -d
```

Check that all services are healthy:

```bash
docker compose ps
```

Expected output:

```
NAME                    STATUS
vartrack-redis-1        running (healthy)
vartrack-mongo-1        running (healthy)
vartrack-orchestrator-api-1    running (healthy)
vartrack-orchestrator-worker-1 running
vartrack-gateway-1      running (healthy)
vartrack-watcher-1      running (healthy)
```

---

## 4. Register the webhook in GitHub

In your GitHub repository go to **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `http://your-server:5657/webhooks/mongo` |
| Content type | `application/json` |
| Secret | The `secret` value from your `config.cue` |
| Events | **Pushes** and **Pull requests** |

Replace `mongo` in the URL with your datasource name (`redis`, `s3`, `configmap`, etc.) if you use a different sink.

Push a change to your config file — varTrack will sync it automatically.

---

## Useful commands

```bash
# Follow all logs
docker compose logs -f

# Follow a single service
docker compose logs -f orchestrator-worker

# Restart a single service
docker compose restart gateway

# Stop everything (keeps volumes)
docker compose down

# Stop and delete all data
docker compose down -v
```

---

## Checking task results

The orchestrator API exposes task status at port 8000:

```bash
# OpenAPI docs
open http://localhost:8000/docs

# Check a specific task
curl http://localhost:8000/v1/tasks/<task-id>

# Prometheus metrics
curl http://localhost:8000/metrics
```

---

## Using an external MongoDB

If you already have a MongoDB, remove the `mongo` service and volume from `docker-compose.yml` and update `config.cue` to point at your connection string:

```cue
datasources: [{
  mongo: {
    endpoint:   "mongodb://user:pass@your-mongo-host:27017"
    database:   "vartrack"
    collection: "variables"
  }
}]
```

---

## Scaling workers

Run more Celery workers to handle higher webhook throughput:

```bash
docker compose up -d --scale orchestrator-worker=3
```

Workers share the `schema-cache` and `git-cache` volumes so clones are reused across replicas.

---

## Disabling drift detection

If you don't need self-heal, remove the `watcher` service from `docker-compose.yml` and omit `self_heal: true` from your rules. This reduces resource usage with no effect on the sync pipeline.
