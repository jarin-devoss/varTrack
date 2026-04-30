# varTrack

**GitOps-native configuration sync.** Push a file to Git — VarTrack writes it to MongoDB, Redis, ZooKeeper, S3, ConfigMaps, Helm, Vercel, or a Linux server automatically. If someone edits the database directly, VarTrack detects it and restores the correct state.

[![CI](https://github.com/jarin-devoss/varTrack/actions/workflows/ci.yml/badge.svg)](https://github.com/jarin-devoss/varTrack/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/jarin-devoss/varTrack/branch/main/graph/badge.svg)](https://codecov.io/gh/jarin-devoss/varTrack)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/docker/v/jarin-devoss/gateway-service?label=docker)](https://hub.docker.com/u/jarin-devoss)

[Quick Start](#quick-start) · [Try the demo](#try-it-in-one-command) · [CLI](cli/README.md) · [Config reference](#configuration-reference--configcue) · [Examples](examples/) · [**Documentation**](https://jarin-devoss.github.io/varTrack/)

---

## What it does

You commit a config file to Git. VarTrack reads it and writes the values to every configured sink:

```yaml
# configs/app.yaml — lives in your Git repo
database_host: mongo.prod.internal
feature_flag_dark_mode: true
max_connections: 50
```

Push to `main` → VarTrack automatically syncs to all configured sinks:

```
MongoDB        → collection "production-config"
Redis          → hash "production:cfg"
S3             → key prefix acme/production/
Kubernetes     → ConfigMap "myapp-production"
Helm           → release values "app-production"
Vercel         → environment variables prefixed "production_"
Linux server   → /etc/app/production.env
ZooKeeper      → znodes under /acme/production/
```

Branch `develop` maps to `staging`, PR #42 maps to `pr-42` — each environment is isolated in every sink automatically.

---

## Try it in one command

The fastest way to see VarTrack in action is the end-to-end demo. It starts a local Git server, MongoDB, ZooKeeper, Redis, and all three VarTrack services pre-configured:

```bash
cd e2e
docker compose up
```

Push a config change to the local repo and watch it sync automatically.

---

## Why VarTrack?

Tools like **ArgoCD** and **FluxCD** are excellent at syncing Kubernetes manifests from Git into a cluster. They don't solve a different problem: syncing runtime configuration values into the datastores your applications actually read at runtime — MongoDB documents, Redis hashes, ZooKeeper znodes, S3 objects, Linux `.env` files.

VarTrack fills that gap. It treats Git as the single source of truth for **application configuration data** — not Kubernetes resources.

| | ArgoCD / FluxCD | VarTrack |
|---|---|---|
| Target | Kubernetes manifests | MongoDB, Redis, ZooKeeper, S3, ConfigMap, Helm, Vercel, Linux |
| Drift detection | Kubernetes state | Any datasource |
| Secret management | External Secrets Operator | HashiCorp Vault + `@secret()` annotations |
| Schema validation | None | CUE per tenant, enforced before every write |

They complement each other: use ArgoCD to deploy your services, use VarTrack to keep their runtime config in sync.

**VarTrack is the right fit if you:**

- Store config, feature flags, or environment variables in any combination of MongoDB, Redis, S3, ZooKeeper, Kubernetes, Helm, Vercel, or Linux servers — and want Git to be the single authority
- Have multiple environments (dev, staging, production) and want changes to flow automatically on push
- Need one push to fan out to multiple sinks at once — write to a ConfigMap, a Helm release, and an S3 object in parallel
- Want drift detection — if someone edits a value directly in any sink, VarTrack restores the correct state automatically
- Need secrets to stay in Vault and never touch Git — `@secret()` annotations resolve at sync time

---

## Features

### Sync
- **8 sink types** — MongoDB, Redis, ZooKeeper, S3, Kubernetes ConfigMap, Helm, Vercel, Linux server (SSH)
- **7 config formats** — YAML, JSON, TOML, .env, INI, HCL, XML — format auto-detected from file extension
- **Multi-sink fan-out** — one push writes to multiple sinks in parallel from one rule set
- **4 sync strategies** — `AUTO` (cost-model picks for you), `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, `LIVE_STATE`
- **Stale key pruning** — automatically delete keys no longer present in Git, with `prune_protection` glob patterns for keys that must never be removed

### Routing & environments
- **`destination_template`** — control exactly where data lands per rule using `{tenant}` and `{env}` placeholders
- **Environment resolution** — map branch names, PR numbers, tags, or file paths to environment names
- **Per-repo overrides** — add a `vartrack.json` to any repository to override rule settings (file paths, branch maps, sync mode, prune) without touching the central bundle
- **Multi-platform** — GitHub webhooks fully supported; GitLab, Bitbucket driver interface ready to extend

### Validation & safety
- **CUE schema validation** — validate every payload against a per-tenant schema before any write
- **Dry-run mode** — simulate any sync without touching a datasource; separate `dry_run_prune` to audit deletes independently
- **`@secret()` annotations** — resolve Vault secrets into config values at ETL time; values masked as `***` in dry-run reports
- **`@logger()` annotations** — emit structured change-log entries when specific fields change value

### Drift detection
- **Drift detection** — watcher-service polls every datasource continuously and alerts on mismatch
- **Self-heal** — set `self_heal: true` on a rule and VarTrack restores correct state automatically on drift
- **Leader election** — ZooKeeper or Redis distributed lock keeps multi-replica watchers in sync

### Operations
- **CLI (`vt`)** — push any local file directly from terminal or CI/CD without a Git push
- **OIDC + RBAC + OPA** — optional auth stack for the CLI API; fine-grained per-env, per-datasource policies
- **Observability** — OpenTelemetry traces, Prometheus metrics, and structured logs with optional ELK shipping on every service

---

## How it works

```mermaid
flowchart LR
    GIT["📁 Git repo\nGitHub / Gitea / GitLab"]
    GW["gateway-service\nGo · :5657"]
    ORCH["orchestrator-service\nPython · :50051\n───────────\nRust ETL core"]
    WATCH["watcher-service\nGo · :9091"]

    GIT -->|"webhook\npush / PR"| GW
    GW -->|"gRPC"| ORCH
    ORCH -->|write| DB["MongoDB · Redis · ZooKeeper\nS3 · ConfigMap · Helm\nVercel · Linux"]
    WATCH -->|"poll\n(drift check)"| DB
    WATCH -->|"TriggerSync\n(self-heal)"| ORCH
```

1. You push a config file to Git.
2. The **gateway-service** (Go) receives the webhook, verifies the HMAC signature, and forwards it via gRPC.
3. The **orchestrator-service** (Python) fetches the file from Git, validates it against a CUE schema, runs the **Rust ETL core** (diff, flatten, merge, prune) for performance-critical operations, and writes to all configured sinks.
4. The **watcher-service** (Go) polls datasources on a configurable interval. When a value diverges from Git, it logs the delta and — if `self_heal: true` — calls the orchestrator to restore correct state.

> **Why Rust inside the orchestrator?** The ETL core (diff, flatten, merge, prune) is implemented in Rust and called from Python via FFI. This keeps the hot path allocation-free and handles multi-megabyte config payloads without GC pressure, while the Python layer handles I/O, Vault, and sink drivers.

| Service | Language | Role |
|---|---|---|
| **gateway-service** | Go | Webhook receiver, platform routing, gRPC forwarder |
| **orchestrator-service** | Python | ETL pipeline, CUE validation, multi-sink writer |
| **rust_core** | Rust (FFI) | Diff, flatten, merge, prune — hot path |
| **watcher-service** | Go | Drift detection, self-heal trigger |
| **vt** | Go | CLI for manual sync and CI/CD integration |

---

## Quick Start

### Option A — Docker Compose (recommended)

The fastest way to run varTrack against your own GitHub repos:

```bash
git clone https://github.com/jarin-devoss/vartrack
cd vartrack
cp .env.example .env          # fill in MONGO_ROOT_PASSWORD at minimum
# write your config.cue (see Configuration Reference below)
docker compose up -d
```

Register a webhook in your GitHub repo pointing to `http://your-server:5657/webhooks/mongo` and push a config file — varTrack syncs it automatically.

See the [Docker Compose guide](docs/deployment/docker-compose.md) for the full walkthrough, `.env` reference, and scaling options.

---

### Option B — Build from source

#### Prerequisites

- Go 1.21+ · Python 3.11+ · a Celery broker (Redis or MongoDB) · at least one configured sink
- `buf` CLI ([install](https://buf.build/docs/installation)) — only needed to build from source

#### 1. Clone and generate proto code

```bash
git clone https://github.com/jarin-devoss/vartrack
cd vartrack
buf generate
```

#### 2. Write your `config.cue`

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
    file_name:            "configs/app.yaml"    // which file in each repo to watch
    repositories:         ["my-org/*"]          // glob pattern — which repos trigger this
    destination_template: "{env}-config"        // collection name resolved at sync time
    self_heal:            true                  // restore on drift automatically
    branch_map: {
      main:    "production"
      develop: "staging"
    }
  }]
}
```

#### 3. Start the services

```bash
# gateway-service
APP_ENV=dev \
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=config.cue \
go run ./gateway-service/cmd

# orchestrator-service (new terminal)
cd orchestrator-service
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8000 &
celery -A app.worker.celery worker --queues=webhooks,sync -l info

# watcher-service (new terminal)
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=config.cue \
go run ./watcher-service/cmd
```

#### 4. Register a webhook in GitHub

Point your repo webhook to `http://your-server:5657/webhooks/mongo`:
- Content type: `application/json`
- Secret: the value from `secret` in your `config.cue`
- Events: **Pushes** and **Pull requests**

Every push now triggers a sync automatically.

---

## `destination_template` — controlling where data lands

This single field controls the storage path, collection name, or key prefix in every sink:

```cue
destination_template: "{env}-config"
// push to "main"    → branch_map resolves "production" → writes to "production-config"
// push to "develop" → branch_map resolves "staging"    → writes to "staging-config"
// PR #42 with env_as_pr: true                          → writes to "pr-42-config"
```

| Sink | What it controls | Example template | Resolved value |
|---|---|---|---|
| MongoDB | Collection name | `"{env}-config"` | `production-config` |
| ZooKeeper | znode root path | `"/{tenant}/{env}"` | `/acme/pr-42` |
| Redis | Key prefix | `"{env}:cfg"` | `production:cfg` |
| S3 | Key prefix | `"{tenant}/{env}/"` | `acme/production/` |
| Linux server | Remote file path | `"/etc/app/{env}.env"` | `/etc/app/production.env` |
| Helm | Release name | `"app-{env}"` | `app-production` |
| ConfigMap | ConfigMap name | `"myapp-{env}"` | `myapp-production` |

---

## Environment resolution

When a webhook fires, VarTrack resolves the target environment in this order:

1. `branch_map[branch]` — explicit mapping (e.g. `main` → `production`)
2. `file_path_map[file_path]` — glob pattern match on the changed file path
3. `env_as_pr: true` → environment = `pr-{number}`
4. `env_as_branch: true` → environment = branch name
5. `env_as_tags: true` → environment = tag name
6. `"default"` fallback

---

## Sync strategies

| Mode | When to use |
|---|---|
| `AUTO` | Recommended — VarTrack picks the strategy based on payload size |
| `GIT_UPSERT_ALL` | Write all keys from Git; best for small configs |
| `GIT_SMART_REPAIR` | Read live state first, write only changed keys; best for large configs |
| `LIVE_STATE` | Replace the entire stored state with Git's current view |

---

## Multi-sink fan-out

Push once, write to multiple sinks in one go:

```cue
rules: [
  { platform: "github", datasource: "mongo",        file_name: "config.yaml", destination_template: "{env}-config" },
  { platform: "github", datasource: "configmap",    file_name: "config.yaml", destination_template: "myapp-{env}" },
  { platform: "github", datasource: "linux_server", file_name: "config.yaml", destination_template: "/etc/app/{env}.env" },
]
```

Any combination works — mix and match MongoDB, Redis, S3, ConfigMap, Helm, Vercel, ZooKeeper, and Linux servers.

---

## Drift detection and self-heal

The watcher-service polls every configured datasource at a set interval. When a value doesn't match the Git baseline:

1. Logs the drift event and increments the `vartrack_watcher_drift_total` Prometheus counter
2. If `self_heal: true` on the rule → calls the orchestrator to re-run the full ETL pipeline
3. Correct state is restored — no manual intervention needed

Running multiple watcher replicas? Enable leader election so only one replica runs the heal loop:

```cue
global_tags: {
  watcher_leader_election_datasource: "redis"  // or "zookeeper"
}
```

---

## CLI — `vt`

Sync any local file directly without a Git push — runs the same ETL pipeline as a webhook:

```bash
# Install
cd cli && go build -o vt ./cmd && mv vt /usr/local/bin/vt

# Authenticate
vt login --server https://gateway.example.com --token eyJ...

# Sync to a Kubernetes ConfigMap
vt sync --file configs/app.yaml --datasource configmap --env production --wait

# Sync to a Helm release
vt sync --file configs/app.yaml --datasource helm --env staging --wait

# Dry-run: see what would be written to S3 without touching anything
vt sync --file configs/app.yaml --datasource s3 --env staging --dry-run

# Validate against the CUE schema (great for PR checks)
vt validate --file configs/app.yaml --datasource linux-server

# Inspect tasks
vt task list
vt task get <task-id>
```

**In GitHub Actions:**

```yaml
- name: Sync config to production
  env:
    VARTRACK_SERVER: https://gateway.example.com
    VARTRACK_TOKEN: ${{ secrets.VARTRACK_TOKEN }}
  run: vt sync --file configs/app.yaml --datasource helm --env production --wait
```

See the full [CLI documentation](cli/README.md) for all commands, flags, and OIDC / SSO login.

---

## Supported config formats

| Extension | Format |
|---|---|
| `.yaml`, `.yml` | YAML |
| `.json` | JSON |
| `.toml` | TOML |
| `.env` | dotenv / KEY=VALUE |
| `.ini` | INI |
| `.hcl` | HCL (Terraform-style) |
| `.xml` | XML |

Format is auto-detected from the file extension.

---

## Full bundle example

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

  datasources: [
    {
      mongo: {
        endpoint:   "mongodb://mongo:27017"
        database:   "vartrack"
        collection: "variables"
      }
    },
    {
      s3: {
        bucket: "my-config-bucket"
        region: "us-east-1"
        access_key_id:     "AKIA..."
        secret_access_key: "..."
      }
    },
    {
      linux_server: {
        host:     "app-server.example.com"
        username: "deploy"
        private_key: "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
      }
    }
  ]

  rules: [
    {
      platform:             "github"
      datasource:           "mongo"
      file_name:            "configs/app.yaml"
      repositories:         ["my-org/*"]
      sync_mode:            "AUTO"
      destination_template: "{env}-config"
      self_heal:            true
      branch_map: {
        main:    "production"
        develop: "staging"
      }
    },
    {
      platform:             "github"
      datasource:           "s3"
      file_name:            "configs/app.yaml"
      repositories:         ["my-org/*"]
      destination_template: "configs/{env}/"
      branch_map: { main: "production", develop: "staging" }
    },
    {
      platform:             "github"
      datasource:           "linux_server"
      file_name:            "configs/app.yaml"
      repositories:         ["my-org/*"]
      destination_template: "/etc/app/{env}.env"
      branch_map: { main: "production", develop: "staging" }
    }
  ]

  schema_registry: {
    platform: "github"
    repo:     "my-org/schemas"
    branch:   "main"
  }

  secret_managers: [{
    vault: {
      endpoint:    "https://vault.mycompany.com"
      mount_point: "secret"
      kv_version:  2
      token_auth: { token: "hvs.xxxx" }
    }
  }]

  // Optional gateway tuning
  max_webhook_body_bytes: 10485760  // 10 MiB (default — omit to keep default)
}
```

---

## API reference

### gateway-service (port 5657)

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/{datasource}` | Receive a platform webhook |
| `POST` | `/webhooks/schema-registry` | Schema-registry push trigger |
| `GET` | `/health/liveness` | Liveness probe |
| `GET` | `/health/readiness` | Readiness probe |

### orchestrator-service (port 8000 HTTP / 50051 gRPC)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/webhooks/{datasource}` | Direct HTTP ingestion |
| `POST` | `/v1/webhooks/{datasource}/dry-run` | Simulate without writing |
| `GET` | `/docs` | OpenAPI / Swagger UI |
| gRPC | `ProcessWebhook` | Called by gateway-service |
| gRPC | `TriggerSync` | Called by watcher-service for self-heal |

---

## Testing

```bash
# gateway-service
cd gateway-service && go test ./...

# watcher-service
cd watcher-service && go test ./...

# orchestrator-service
cd orchestrator-service
pip install -e ".[test]"
pytest tests/

# End-to-end (Docker Compose)
cd e2e && docker compose up
```

---

## Docker

```bash
docker build -t jarin-devoss/gateway-service:latest      ./gateway-service
docker build -t jarin-devoss/orchestrator-service:latest ./orchestrator-service/docker/Dockerfile \
  --file ./orchestrator-service/docker/Dockerfile ./orchestrator-service
docker build -t jarin-devoss/watcher-service:latest      ./watcher-service
```

Pre-built multi-arch images (`linux/amd64`, `linux/arm64`) are published on every tagged release:

```bash
docker pull jarin-devoss/gateway-service:latest
docker pull jarin-devoss/orchestrator-service:latest
docker pull jarin-devoss/watcher-service:latest
```

---

## Repository layout

```
varTrack/
├── config.cue                    # Shared CUE bundle (all services read this)
├── proto/                        # Protobuf definitions
├── buf.gen.yaml                  # Buf code-generation config
│
├── gateway-service/              # Go — HTTP ingress, webhook receiver
├── orchestrator-service/         # Python + Rust — ETL pipeline, sink writer
├── watcher-service/              # Go — drift detection, self-heal
├── cli/                          # Go — vt command-line tool
├── examples/                     # Example bundles and config files
└── e2e/                          # End-to-end demo + test suite
```

---

## Security

Webhook signatures (HMAC-SHA256), replay protection, rate limiting, and mTLS are enforced automatically in production (`APP_ENV=production`). See [SECURITY.md](SECURITY.md) for the full security model.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).
