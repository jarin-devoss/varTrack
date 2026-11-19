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

No manual setup needed. Starts everything in Docker:

```bash
git clone https://github.com/jarin-devoss/varTrack
cd varTrack/demo
docker compose up
```

This starts a local Git server, MongoDB, ZooKeeper, Redis, and all three varTrack services with a pre-configured bundle. It seeds a config repository once and syncs it, so the UI opens on rules with environments already written. Push any config change to the local repo and watch it sync; restarting the demo keeps what you pushed (`docker compose down -v`, or `./run.sh reset`, starts again from nothing). [demo/README.md](https://github.com/jarin-devoss/varTrack/blob/main/demo/README.md) has a short tour. It needs Docker with Compose 2.20.0 or newer and about 2 GiB of memory; on Linux install Docker's own packages, not a distribution's `docker-compose` (Compose V1), which does not run it. The [Requirements](https://github.com/jarin-devoss/varTrack/blob/main/demo/README.md#requirements) there also say which ports it uses and how to open a demo that runs on a server. It also serves the web UI at http://localhost:8080 (sign in with Gitea as `demo-admin`, password `VarTrack123`).

`docker compose up` reuses an image that already exists, however old. After you pull new code, add `--build`, or the UI can be newer than the orchestrator behind it and show a blank page. `node e2e/stale-images.mjs` (from the repository root) names every image of a running stack that is older than its sources.

Image builds download dependencies over TLS (`buf` plugins, Go modules, pip and npm packages). On a machine where an antivirus or proxy re-signs HTTPS, the build stops with `x509: certificate signed by unknown authority`: exclude the build hosts from the scan, or see [Run the stack on a machine that cannot build the images](https://github.com/jarin-devoss/varTrack/blob/main/ui/e2e/README.md#run-the-stack-on-a-machine-that-cannot-build-the-images).

### With more sinks and the web UI

`demo/docker-compose.live.yml` adds S3 on MinIO and Kubernetes ConfigMaps and Helm releases on k3s, and seeds every sink through real Gitea pushes and webhooks:

```bash
cd demo
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d --build \
  gitea redis mongo zookeeper minio minio-init vault vault-init \
  k3s k3s-kubeconfig orchestrator-api orchestrator-worker gateway watcher live-seed

cd ../ui && npm install && npm run dev
```

The images generate their own Python and Go protobuf stubs, so `buf generate` on the host is not needed to run the stack. Open the UI at http://localhost:5173 (this stack's sign-in redirects there, so the dev server must be running; the plain demo's is http://localhost:8080). Two Git servers feed the demo, so sign-in covers two platforms: **gitea** (http://localhost:3000, the demo configs) and **gitea-team** (http://127.0.0.1:3001, a payments team with a private repository). Sign in with one and connect the other — every password is `VarTrack123`:

| Sign in to gitea as… | then connect gitea-team as… | you see |
|---|---|---|
| `demo-admin` | `team-admin` | all 7 rules on one page, and can sync them: the 6 demo rules of tenant `default` and the payments rule of tenant `payments`, with no tenant to switch to |
| `demo-viewer` | `demo-viewer` | the 6 demo rules, dry runs only (Sync is disabled and says why; a dry run's log shows in the panel); the payments rule stays hidden — not in that team |

The demo's rule settings come in layers ([how a rule is built](configuration/schema-registry.md#how-a-rule-is-built)):

| Layer | In the demo |
|---|---|
| `config.cue` | `demo/live/config.cue`: every rule, the datasources' connections, Vault |
| a tenant's `bundle.json` | tenant `default`: `vartrack-demo/schemas` on gitea, `extends` the `production` preset · tenant `payments`: `payments/schemas` on gitea-team, `extends` the `team` preset on gitea |
| a repo's `vartrack.json` | `demo-configs` has one on `main` and a different one on `develop`; `payments-config` has `.gitea/vartrack.json`, extending a preset in the payments team's own schema repo |
| presets | `vartrack-demo/presets` (`demo/live/presets`): each layer adds `prune.protection` patterns |

Every environment shows its desired state in Git next to the current state in its sink. See [Web UI sign-in](configuration/auth.md#web-ui-sign-in-with-your-git-platform) to turn sign-in off or set it up for your own platforms.

![A rule's environments: staging differs from Git in S3, production matches](assets/screenshots/rule-environments.png)

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

What `vt` may do is what your Git token may do in the rule's repositories: push access syncs, read access validates and dry-runs — see [Authentication & Authorization](configuration/auth.md). In the live demo, make a Gitea token and use it:

```bash
TOKEN=$(curl -s -u demo-admin:VarTrack123 -X POST -H "Content-Type: application/json" \
  -d '{"name":"vt","scopes":["read:user","read:repository"]}' \
  http://localhost:3000/api/v1/users/demo-admin/tokens | python3 -c "import sys,json;print(json.load(sys.stdin)['sha1'])")
vt login --server http://localhost:5657 --token "$TOKEN"
vt sync --file app-settings.yaml --datasource mongo-primary --env staging --wait   # the live demo's rule tracks app-settings.yaml
```

`demo-admin` administers the demo repositories, so it can sync anywhere; `demo-viewer` can only read them, so it can validate and dry-run but not sync ([the example OPA policy](https://github.com/jarin-devoss/varTrack/blob/main/examples/policies/vartrack_authz.rego) adds: writers never sync production or `configs/secrets/*`). The same token, pasted into the *Git token* card on the UI's Playground page, drives the Playground.

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
