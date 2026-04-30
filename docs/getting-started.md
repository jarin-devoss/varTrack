# Getting Started

Get varTrack running locally in under 10 minutes.

---

## Prerequisites

- Go 1.21+
- Python 3.11+
- A Celery broker — Redis or MongoDB (configured via `celery_broker_datasource` in the bundle)
- At least one configured sink
- [`buf` CLI](https://buf.build/docs/installation) — for proto code generation

---

## Option A — E2E demo (fastest)

No manual setup needed. Starts everything in Docker:

```bash
git clone https://github.com/jarin-devoss/varTrack
cd varTrack/e2e
docker compose up
```

This starts a local Git server, MongoDB, ZooKeeper, Redis, and all three varTrack services with a pre-configured bundle. Push any config change to the local repo and watch it sync.

---

## Option B — Build from source

### 1. Clone and generate proto code

```bash
git clone https://github.com/jarin-devoss/varTrack
cd varTrack
buf generate
```

### 2. Write `config.cue`

All three services share one CUE bundle that declares your platforms, datasources, and rules.

```cue
bundle: {
  platforms: [{
    github: {
      endpoint:        "https://github.com"
      push_event_name: "push"
      pr_event_name:   "pull_request"
      secret:          "my-webhook-secret"
    }
  }]

  datasources: [{
    mongo: {
      endpoint:   "mongodb://localhost:27017"
      database:   "vartrack"
      collection: "variables"
    }
  }]

  rules: [{
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/app.yaml"   // file to watch in each repo
    repositories:         ["my-org/*"]         // glob — which repos trigger this rule
    destination_template: "{env}-config"       // collection name resolved at sync time
    self_heal:            true
    branch_map: {
      main:    "production"
      develop: "staging"
    }
  }]
}
```

### 3. Start the gateway-service

```bash
APP_ENV=dev \
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=config.cue \
go run ./gateway-service/cmd
```

Listens on `:5657` (webhooks) and `:9090` (metrics/health).

### 4. Start the orchestrator-service

```bash
cd orchestrator-service
pip install -e .

# API + gRPC server
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Celery worker (separate terminal)
celery -A app.worker.celery worker --queues=webhooks,sync -l info
```

### 5. Start the watcher-service

```bash
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=config.cue \
go run ./watcher-service/cmd
```

Polls configured datasources every 60 seconds by default.

### 6. Register a webhook in GitHub

Point your repository webhook to `http://your-server:5657/webhooks/mongo`:

| Setting | Value |
|---|---|
| Payload URL | `http://your-server:5657/webhooks/mongo` |
| Content type | `application/json` |
| Secret | Value of `secret` from `config.cue` |
| Events | Pushes + Pull requests |

Every push to a matching repository now triggers a sync automatically.

---

## Verify it works

Push a change to a tracked repository, then check MongoDB:

```bash
mongosh vartrack --eval 'db["production-config"].find().pretty()'
```

You should see your config values written as key-value documents.

---

## Install the CLI

The `vt` CLI lets you sync files directly without a Git push:

```bash
cd cli
go build -o vt ./cmd
mv vt /usr/local/bin/vt

# Authenticate
vt login --server http://localhost:5657 --token eyJ...

# Push a file manually
vt sync --file configs/app.yaml --datasource mongo --env staging --wait
```

[:octicons-arrow-right-24: Full CLI reference](cli/index.md)

---

## Next steps

<div class="grid cards" markdown>

-   [:material-swap-horizontal: **Destination template**](concepts/destination-template.md)

    Control exactly where data lands using `{env}` and `{tenant}` placeholders

-   [:material-source-branch: **Environment resolution**](concepts/environment-resolution.md)

    Map branch names, PR numbers, and tags to environment names

-   [:material-database-sync: **All sink types**](sinks/index.md)

    Configure MongoDB, Redis, ZooKeeper, S3, and more

-   [:material-radar: **Drift detection**](concepts/drift-detection.md)

    Understand how the watcher detects and heals config drift

</div>
