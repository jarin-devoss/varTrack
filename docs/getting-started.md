# Getting Started

Get varTrack running locally in under 10 minutes.

---

## Prerequisites

For the demo (Option A), only Docker with Compose 2.20.0 or newer. To build from source (Option B):

- Go 1.26+
- Python 3.11+
- Git
- Redis: the Celery broker, which also holds the sync ledger and the locks (`CELERY_BROKER_URL`, or a Redis datasource named by `celery_broker_datasource` in the bundle)
- At least one configured sink
- [`buf` CLI](https://buf.build/docs/installation) — for proto code generation
- The [`cue` CLI](https://cuelang.org/docs/introduction/installation/), v0.6.0 or newer — the orchestrator validates files with it

---

## Option A — the demo (fastest)

Everything runs in Docker; nothing else is installed:

```bash
git clone https://github.com/jarin-devoss/varTrack
cd varTrack/demo
docker compose up
```

- It starts a local Git server (Gitea), MongoDB, Redis, ZooKeeper, S3 on MinIO, a Linux server over SSH, Vault and the three varTrack services, seeds a config repository and syncs it.
- Open the web UI at http://localhost:8080 and sign in with Gitea as `demo-admin`, password `VarTrack123`. Push a change to the local repository and watch it sync.
- It needs Docker with Compose 2.20.0 or newer and about 2 GiB of memory. [demo/README.md](https://github.com/jarin-devoss/varTrack/blob/main/demo/README.md) has the ports, a short tour, and how to reset it.
- After you pull new code, run `docker compose up --build`: an older image keeps running otherwise.
- If the build stops with `x509: certificate signed by unknown authority`, an antivirus or proxy re-signs HTTPS on this machine: see [Run the stack on a machine that cannot build the images](https://github.com/jarin-devoss/varTrack/blob/main/ui/e2e/README.md#run-the-stack-on-a-machine-that-cannot-build-the-images).

To also see Kubernetes ConfigMaps and Helm releases, a second Git server and rules built in layers, run [the full demo](guides/full-demo.md).

---

## Option B — Build from source

### 1. Clone, generate proto code, and build the config loader

```bash
git clone https://github.com/jarin-devoss/varTrack
cd varTrack
buf generate

# vartrack-bundle reads config.cue for the orchestrator, exactly as the gateway and the watcher read it
(cd libs/go && go install ./cmd/vartrack-bundle)
export PATH="$(go env GOPATH)/bin:$PATH"     # or set VARTRACK_BUNDLE_BINARY to the file
```

Without `vartrack-bundle` the orchestrator refuses to start: it never runs on a `config.cue` the gateway would not accept.

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

Each Go service is a module of its own, so it is run from its own folder:

```bash
cd gateway-service
APP_ENV=dev \
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=../config.cue \
go run ./cmd
```

Listens on `:5657` (webhooks) and `:9090` (metrics/health).

### 4. Start the orchestrator-service

```bash
cd orchestrator-service
pip install -e .
export CONFIG_PATH=../config.cue PYTHONPATH=app/generated/proto/python   # the stubs buf generate wrote

# API + gRPC server
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Celery worker (separate terminal)
python -m app.worker
```

### 5. Start the watcher-service

```bash
cd watcher-service
ORCHESTRATOR_ADDR=localhost:50051 \
CONFIG_PATH=../config.cue \
go run ./cmd
```

Every 60 seconds by default, asks the orchestrator whether each `self_heal` rule's datasource still matches Git.

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

You should see one document for the file, with your config values nested under `data` (the default `STRATEGY_DOCUMENT`; set `update_strategy: "STRATEGY_KEY_VALUE"` for one document per key instead).

---

## Install the CLI

The `vt` CLI lets you sync files directly without a Git push:

```bash
cd cli
go build -o vt ./cmd
mv vt /usr/local/bin/vt

# Authenticate with your Git token (GitHub or Gitea)
vt login --server http://localhost:5657 --token <your Git token>

# Push a file manually
vt sync --file configs/app.yaml --datasource mongo --env staging --wait
```

What `vt` may do is what your Git token may do in the rule's repositories: push access syncs, read access validates and dry-runs — see [Authentication & Authorization](configuration/auth.md). The [full demo](guides/full-demo.md#the-cli-in-the-full-demo) shows how to make a token for its Gitea and what each demo user may do.

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

- [Configuration files](configuration/index.md) — where `config.cue`, `bundle.json` and `vartrack.json` live, and how they make a rule
- [Onboard a config repository](guides/onboard-a-repository.md) — your own repository, end to end
- [Production checklist](operations/production-checklist.md) — before syncing production
