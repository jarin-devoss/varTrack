# How varTrack Works

varTrack is built around a simple idea: your Git repository is the source of truth for configuration, and any datasource that holds config values should automatically stay in sync with it.

---

## The pipeline

Every sync — whether triggered by a webhook or the CLI — runs through the same pipeline. A CLI sync brings the file itself, so it skips fetching from Git: it starts at the rule for its datasource and goes straight to parsing.

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

- Which **platform** sent the event (GitHub or Gitea)
- Which **rule** applies: the one for the platform that sent the event and the datasource in the webhook URL (a rule is identified by that pair). Its `repositories` and `exclude_repositories` decide whether the pushed repository is synced, and `file_name` or `file_path_map` which files
- Which **environment** to target (from branch name, PR number, tag, or explicit `branch_map`)

### Stage 2 — ETL

The orchestrator-service:

1. Reads the files at the pushed commit, from a bare mirror of the repository kept under `GIT_CACHE_DIR` (at most 20 repos, least recently used removed first)
2. Parses the file (YAML / JSON / TOML / .env / HCL / XML / INI)
3. Extracts the subtree under the `vartrack` root key (if present)
4. Reads the settings of the environment being synced out of the file ([several environments in one file](environment-resolution.md#several-environments-in-one-file))
5. Flattens nested config to `{ "key.subkey": "value" }`
6. Applies `variables_map` overlay if configured
7. Validates against the CUE schema for this tenant

### Stage 3 — Sync

Writes the validated flat map to every configured sink:

- Takes a slot of the datasource's [sync limit](../configuration/bundle.md#sync_limits), if `config.cue` gives it one, and waits for one when none is free
- Takes the lock of the place it writes: one sync at a time writes a place, whatever the number of workers
- Leaves a file alone when a newer commit of it is already written there ([below](#an-older-commit-never-overwrites-a-newer-one))
- Picks the sync strategy (`AUTO`, `GIT_UPSERT_ALL` or `GIT_SMART_REPAIR`)
- Runs writes to all matching sinks in parallel
- Prunes stale keys once per environment, after all its files are written, if `prune` is enabled on the rule
- Rolls back (deletes written data) if a write fails partway through

### An older commit never overwrites a newer one

Two pushes to one branch make two syncs, and the workers may run them in either order. Each file written to a place has its commit remembered, and a sync that is about to write a file asks Git whether the commit already written is a descendant of its own. If it is, its own is the older one: the file is left alone, the sync is still a success, and the file says why:

```text
skipped: commit 3f9a1c0d2b4e is newer than 91be7d02c5aa and is already written to production
```

It writes when the commits are the same, when its own is the newer one, when the history is unrelated (a force push), and whenever nothing can say (no record, Redis or the commit cannot be reached). Skipping is only ever for a commit Git itself says is older. The record is per file and per place: a newer commit that did not write a file is no reason to skip an older one that did.

A push from the CLI (`vt sync`) carries a label rather than a commit, so it is not ordered.

---

## Services

| Service | Language | Port | Role |
|---|---|---|---|
| **gateway-service** | Go | 5657 | HTTP webhook receiver, platform routing, gRPC forwarder |
| **orchestrator-service** | Python | 8000 / 50051 | ETL pipeline, CUE validation, multi-sink writer |
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
                    │  │  read files at pushed commit    │  │
                    │  │  parse + flatten                │  │
                    │  │  validate CUE schema            │  │
                    │  │  write to sinks                 │──┼──► MongoDB
                    │  └────────────────────────────────┘  │──► Redis
                    └──────────────────────────────────────┘──► ZooKeeper
                                                               ──► S3 / ...
                    ┌──────────────────────────────────────┐
                    │          watcher-service              │
                    │  every 60 s, per self_heal rule:     │
                    │  CheckDatasource (gRPC) — the        │
                    │  orchestrator compares sink vs Git   │
                    │  drifted → SyncDatasource (gRPC)     │
                    └──────────────────────────────────────┘
```

---

## The ETL core

The ETL runs in Python: parse the file, read the environment being synced out of it, flatten it to dotted keys, validate it against the CUE schema, compare it with the sink, and write. There is one flatten (`transformer._py_flatten`): it walks the file with an explicit stack, so a deeply nested file cannot overflow the call stack.
---

## Next

- [Destination template](destination-template.md) — control where data lands per rule
- [Environment resolution](environment-resolution.md) — map branches, PRs, and tags to environments
- [Sync strategies](sync-strategies.md) — choose how writes are applied
- [Drift detection](drift-detection.md) — how the watcher catches and heals config drift
