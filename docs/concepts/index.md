# How varTrack Works

varTrack is built around a simple idea: your Git repository is the source of truth for configuration, and any datasource that holds config values should automatically stay in sync with it.

---

## The pipeline

Every sync — whether triggered by a webhook or the CLI — runs through the same three-stage pipeline:

```
PAYLOAD       →   ETL             →   SYNC
──────────────────────────────────────────────
resolve rule      fetch from Git      pick strategy
match platform    parse file          write to sinks
                  flatten keys        prune stale keys
                  validate CUE        rollback on error
```

### Stage 1 — Payload

When a webhook arrives at the gateway-service, varTrack resolves:

- Which **platform** sent the event (GitHub, GitLab, ...)
- Which **rule** matches (based on `datasource`, `repositories`, and `file_name`)
- Which **environment** to target (from branch name, PR number, tag, or explicit `branch_map`)

### Stage 2 — ETL

The orchestrator-service:

1. Clones or fetches the pushed Git ref (LRU cache, max 20 repos)
2. Parses the file (YAML / JSON / TOML / .env / HCL / XML / INI)
3. Extracts the subtree under the `vartrack` root key (if present)
4. Detects the environment key pattern (branch-keyed or flat)
5. Flattens nested config to `{ "key.subkey": "value" }` using Rust BFS/DFS
6. Applies `variables_map` overlay if configured
7. Validates against the CUE schema for this tenant

### Stage 3 — Sync

Writes the validated flat map to every configured sink:

- Picks the sync strategy (`AUTO`, `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, or `LIVE_STATE`)
- Runs writes to all matching sinks in parallel
- Prunes stale keys if `prune: true` is set on the rule
- Rolls back (deletes written data) if a write fails partway through

---

## Services

| Service | Language | Port | Role |
|---|---|---|---|
| **gateway-service** | Go | 5657 | HTTP webhook receiver, platform routing, gRPC forwarder |
| **orchestrator-service** | Python + Rust | 8000 / 50051 | ETL pipeline, CUE validation, multi-sink writer |
| **watcher-service** | Go | 9091 | Drift detection, self-heal trigger |
| **vt** | Go | — | CLI for manual sync and CI/CD |

---

## Data flow diagram

```
                    ┌──────────────────────────────────────┐
  GitHub push  ───► │          gateway-service             │
                    │  ┌────────────────────────────────┐  │
                    │  │  verify signature               │  │
                    │  │  match rule                     │  │
                    │  │  rate limit                     │  │
                    │  └─────────────┬──────────────────┘  │
                    └────────────────┼─────────────────────┘
                                     │ gRPC
                    ┌────────────────▼─────────────────────┐
                    │        orchestrator-service           │
                    │  ┌────────────────────────────────┐  │
                    │  │  fetch Git ref (LRU cache)      │  │
                    │  │  parse + flatten (Rust)         │  │
                    │  │  validate CUE schema            │  │
                    │  │  write to sinks                 │──┼──► MongoDB
                    │  └────────────────────────────────┘  │──► Redis
                    └──────────────────────────────────────┘──► ZooKeeper
                                                               ──► S3 / ...
                    ┌──────────────────────────────────────┐
                    │          watcher-service              │
                    │  polls datasources every 60 s        │
                    │  compares live state vs Git baseline  │
                    │  drift detected → TriggerSync (gRPC) │
                    └──────────────────────────────────────┘
```

---

## Rust core

Hot-path operations (flattening, merging, diffing, hashing) are implemented in Rust and exposed to Python via PyO3. Pure-Python fallbacks activate automatically if the Rust extension is not compiled — so the service always works, just slightly slower without it.

| Module | Purpose |
|---|---|
| `flatten` | BFS / DFS nested dict → flat `key.subkey` map |
| `merge` | Variable map overlay + environment resolution |
| `prune` | Stale-key detection and deferred deletion |
| `sync` | BLAKE3 content hash for `GIT_SMART_REPAIR` |

---

## Next

- [Destination template](destination-template.md) — control where data lands per rule
- [Environment resolution](environment-resolution.md) — map branches, PRs, and tags to environments
- [Sync strategies](sync-strategies.md) — choose how writes are applied
- [Drift detection](drift-detection.md) — how the watcher catches and heals config drift
