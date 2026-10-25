# varTrack

**GitOps-native configuration sync.** Push a file to Git — varTrack writes it to your datastores automatically.

[![CI](https://github.com/jarin-devoss/varTrack/actions/workflows/ci.yml/badge.svg)](https://github.com/jarin-devoss/varTrack/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/jarin-devoss/varTrack/blob/main/LICENSE)
[![Docker](https://img.shields.io/docker/v/jarin-devoss/gateway-service?label=docker)](https://hub.docker.com/u/jarin-devoss)

---

## What it does

You commit a config file. varTrack syncs it everywhere.

```yaml
# configs/app.yaml — lives in your Git repo
database_host: mongo.prod.internal
feature_flag_dark_mode: true
max_connections: 50
```

Push to `main` → varTrack automatically writes these values to every configured sink:

```
MongoDB     →  collection "production-config"
Redis       →  hash       "production:cfg"
ZooKeeper   →  znodes     /acme/production/
S3          →  key prefix acme/production/
```

If someone edits MongoDB directly and changes `max_connections` to `5`, the watcher detects it and restores `50` automatically — no manual intervention needed.

---

## Why varTrack?

Git is a good home for application configuration, but the values your applications read live in datastores — MongoDB documents, Redis hashes, ZooKeeper znodes, S3 objects, Linux `.env` files — and nothing keeps them in step with Git.

varTrack fills that gap. It treats Git as the single source of truth for **application configuration data**, and writes it to the datastores your applications actually read.

| | varTrack |
|---|---|
| Target | MongoDB, Redis, ZooKeeper, S3, ConfigMap, Helm, Vercel, Linux |
| Trigger | Git push / webhook |
| Drift detection | Any datasource |
| Secret management | HashiCorp Vault integration |
| Schema validation | CUE schema per tenant |

It works alongside whatever deploys your services: varTrack only keeps their runtime config in sync.

---

## Key features

<div class="grid cards" markdown>

-   :material-database-sync:{ .lg .middle } **8 sink types**

    ---

    MongoDB, Redis, ZooKeeper, S3, Kubernetes ConfigMap, Helm, Vercel, Linux server (SSH)

    [:octicons-arrow-right-24: See all sinks](sinks/index.md)

-   :material-shield-lock:{ .lg .middle } **Secret management**

    ---

    `@secret()` annotations pull values from HashiCorp Vault at ETL time. Secrets never touch Git. Dry-run reports mask them as `***`

    [:octicons-arrow-right-24: Schema annotations](configuration/annotations.md)

-   :material-check-decagram:{ .lg .middle } **CUE schema validation**

    ---

    Every push is validated against a per-tenant CUE schema before any write. Invalid configs are rejected with a detailed error — zero bad writes

    [:octicons-arrow-right-24: Schema annotations](configuration/annotations.md)

-   :material-radar:{ .lg .middle } **Drift detection**

    ---

    The watcher asks the orchestrator each poll whether a datasource still matches Git. On drift, it logs the delta and optionally restores correct state — automatically

    [:octicons-arrow-right-24: How drift detection works](concepts/drift-detection.md)

-   :material-swap-horizontal:{ .lg .middle } **Multi-sink fan-out**

    ---

    One push writes to multiple sinks in parallel. One rule set for all environments

    [:octicons-arrow-right-24: Destination template](concepts/destination-template.md)

-   :material-source-branch:{ .lg .middle } **Any config format**

    ---

    YAML, JSON, TOML, .env, INI, HCL, XML — format auto-detected from file extension

    [:octicons-arrow-right-24: Supported formats](configuration/formats.md)

-   :material-console:{ .lg .middle } **CLI — `vt`**

    ---

    Push any local file from terminal or CI/CD. Validate against schemas. Inspect tasks

    [:octicons-arrow-right-24: CLI reference](cli/index.md)

-   :material-eye-check:{ .lg .middle } **Dry-run mode**

    ---

    Simulate any sync and see exactly what would be written — without touching a datasource. Secret fields masked as `***`

    [:octicons-arrow-right-24: Dry-run reference](concepts/sync-strategies.md#dry-run)

</div>

---

## Try it in one command

The fastest way to see varTrack in action is the end-to-end demo. It starts a local Git server, MongoDB, ZooKeeper, Redis, and all three varTrack services:

```bash
cd e2e
docker compose up
```

Push a config change to the local repo and watch it sync automatically.

---

## How it works

```mermaid
flowchart LR
    GIT["📁 Git repo\n(GitHub / Gitea / GitLab)"]
    GW["gateway-service\nGo · :5657"]
    ORCH["orchestrator-service\nPython · :50051\n─────────────\nRust ETL core"]
    WATCH["watcher-service\nGo · :9091"]

    GIT -->|"webhook\n(push / PR)"| GW
    GW -->|"gRPC\nProcessWebhook"| ORCH

    ORCH -->|write| MONGO["MongoDB"]
    ORCH -->|write| REDIS["Redis"]
    ORCH -->|write| ZK["ZooKeeper"]
    ORCH -->|write| S3["S3 / MinIO"]
    ORCH -->|write| OTHER["ConfigMap · Helm\nVercel · Linux"]

    WATCH -->|"CheckDatasource\n(drift check)"| ORCH
    WATCH -->|"SyncDatasource\n(self-heal)"| ORCH
```

1. You push a config file.
2. **gateway-service** (Go) receives the webhook, verifies the HMAC signature, and forwards it via gRPC.
3. **orchestrator-service** (Python) fetches the file from Git, validates it against a CUE schema, runs the Rust ETL core (diff, merge, prune), and writes to all configured sinks.
4. **watcher-service** (Go) asks the orchestrator on a configurable interval whether each `self_heal` rule's datasource still matches Git — the orchestrator reads the sink — and on drift has it restore correct state.

!!! info "Why Rust inside the orchestrator?"
    The ETL core (diff, flatten, merge, prune) is implemented in Rust and called from Python via FFI. This keeps the hot path allocation-free and handles multi-megabyte config payloads without GC pressure, while the Python layer handles I/O, Vault, and sink drivers.

[:octicons-arrow-right-24: Full architecture overview](concepts/index.md)
