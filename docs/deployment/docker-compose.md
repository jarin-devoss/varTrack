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

  datasources: [
    {mongo: {
      endpoint:   "mongodb://vartrack:your-strong-password@mongo:27017"
      database:   "vartrack"
      collection: "variables"
    }},
    {redis: {
      hosts:    ["redis:6379"]
      database: 0
    }},
  ]

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

!!! note "The end-to-end demo stack runs with internal authentication on"
    `e2e/docker-compose.yml` sets `INTERNAL_AUTH_SECRET` on every service, so the orchestrator answers
    `401` to a call without a signed grant, `/docs` and `/metrics` on port 8000 included (only the
    health probes are open). Mint a grant from inside the stack and pass it:

    ```bash
    GRANT=$(docker compose -f e2e/docker-compose.yml exec -T orchestrator-api \
              python -m app.api.tokens mint --audience sync --ttl 600)
    curl -H "Authorization: Bearer $GRANT" http://localhost:8000/metrics
    ```

---

## The end-to-end regression

`e2e/docker-compose.yml` runs the whole product (Gitea, Vault, MongoDB, Redis, ZooKeeper, MinIO, an SSH target,
the gateway, the orchestrator and the watcher) and then the 23 suites of `e2e/init/test_runner.py`:

```bash
cd e2e
docker compose up --build --detach
docker compose wait e2e-demo       # the exit code is the regression's: 0 only when every suite passed
python3 check_results.py           # 23 suites, none failed, none skipped; prints the table
```

- **A suite that fails fails the run.** `e2e-demo` exits with the runner's status (`demo.sh` no longer turns a failure
  into a warning), CI checks the report as well (`e2e/check_results.py`), and the report (`e2e/results/benchmark.json`)
  is not kept in git, so a run that wrote nothing cannot pass on an old one.
- **Suites wait for the task they started, never for the clock.** Each polls `GET /v1/tasks/{id}` until the task
  reports it has finished (`E2E_TIMEOUT`, default 60 s, is only the ceiling for a hung one), then asserts what the
  task says: its state, the files it synced and the **schema commit it validated against**, which has to be the
  commit the suite pushed. A whole run takes about half a minute.
- **A run does not depend on the one before it.** Every suite starts by emptying the destinations it writes to (its own
  collection, key prefix, znode path, file or object prefix), so running the regression again on the same stack
  passes. `docker compose down -v` is still the way to start from nothing.
- **Nothing is retried.** A suite that fails one run in a few is a bug: the scheduled `e2e-repeat` workflow runs the
  regression several times in a row on fresh volumes to find it.

### The web UI in the demo

The same stack serves the web UI on <http://localhost:8080> with sign-in through the demo's Gitea (<http://localhost:3000>).
The demo creates four Gitea accounts, all with the password `VarTrack123`, to see the UI as each kind of person:

| Account | What they are |
|---|---|
| `demo-admin` | a varTrack administrator (`ui.auth.admins` in `config.cue`): sees and syncs every rule |
| `demo-writer` | can push to the config repository, except to its protected `main` branch |
| `demo-viewer` | can read the config repository |
| `demo-outsider` | can read none of it (the config repository is private): sees no rule |

Open the UI, choose *Sign in with Gitea*, and sign in as one of them; Gitea asks for consent the first time.

The demo is a test stack, so it lowers two timers: the watcher looks every `10s` (`POLL_INTERVAL`; the product default is `60s`) and the web UI re-reads a sink after `2` seconds (`UI_COMPARE_TTL_SECONDS`; the default is `10`), so that a test waits seconds, not minutes. Set `WATCHER_POLL_INTERVAL` to change the first.

The UI has its own end-to-end suite, run against this stack by the `ui-e2e` job in CI. See
[`ui/e2e/README.md`](https://github.com/jarin-devoss/varTrack/blob/main/ui/e2e/README.md) to run it.

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
