# Drift Detection & Self-Heal

The watcher-service continuously polls every configured datasource and compares the live state against the Git baseline. When values diverge, it can restore the correct state automatically.

---

## What is drift?

Drift happens when a datasource value no longer matches what Git says it should be. Common causes:

- A developer edits MongoDB directly during an incident
- A deployment script overwrites a config value
- A Kubernetes operator changes a ConfigMap
- A Redis key expires or is flushed

Without drift detection, these changes go unnoticed until something breaks.

---

## How detection works

The watcher reads each datasource and keeps a fingerprint of it. A fingerprint only says *that* something changed — varTrack's own syncs change it too — so when it moves, the watcher asks the orchestrator whether the datasource still matches Git, key by key (`CheckDatasource`). Only a mismatch is drift.

```
Every POLL_INTERVAL (default: 60s)
  │
  ├── Fingerprint the datasource
  ├── Same as the baseline → nothing to do
  │
  └── Changed → CheckDatasource: compare every synced environment with the
      desired state varTrack last wrote from Git
        ├── in sync  → a varTrack sync wrote it: new baseline, no heal
        ├── syncing  → a sync is still writing: look again on the next poll
        └── drifted  → (also: orchestrator can't compare, see below)
              ├── Increment vartrack_watcher_drift_total
              └── Heal: SyncDatasource gRPC → orchestrator re-syncs this
                  datasource from the head of every branch that feeds it
```

A few rules keep this from healing what it shouldn't, or waiting forever:

- **Unknown** — the orchestrator answers but can't compare (nothing synced yet, or it can't read the sink back, as with Vercel): the watcher heals, as it did before the check existed. A heal only ever writes what Git says.
- **Orchestrator unreachable or older** (no `CheckDatasource`): the watcher heals on any change, as before.
- **A sync that died** — a worker killed mid-sync leaves no result. A sync still marked running after 15 minutes (Celery stops any task after 10) no longer counts, so the watcher heals instead of waiting on it.
- **First start without a saved baseline** — the watcher asks once right away, so drift from before it ever ran is healed too. At startup only a definite *drifted* heals; with no change seen, *unknown* is no evidence.
- **A failed heal** is not retried until the datasource changes again: the same sync would fail the same way. The web UI shows it as *Heal failed*.

---

## Enabling self-heal

Set `self_heal: true` on a rule to enable automatic repair:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  self_heal:  true    // watcher will restore state on drift
}]
```

Rules with `self_heal: false` (or omitted) are still polled and drift is logged, but no automatic repair is triggered.

---

## Concrete example

Your config in Git:

```yaml
max_connections: 50
log_level: info
```

Someone runs `db.variables.updateOne({key: "max_connections"}, {$set: {value: 5}})` directly in MongoDB.

On the next poll cycle:

1. Watcher sees the MongoDB fingerprint change
2. Asks the orchestrator (`CheckDatasource`): Git says `50`, MongoDB has `5` — drifted
3. `self_heal: true` → calls `SyncDatasource` on the orchestrator
4. Orchestrator fetches the file from Git, parses it, writes `max_connections = 50` back to MongoDB
5. That write moves the fingerprint again; the orchestrator says it matches Git, so it becomes the new baseline — state restored

Had the change come from a push to Git, step 2 would have answered *in sync* and nothing would be healed.

Total recovery time: up to one `POLL_INTERVAL` (default 60 seconds).

---

## Metrics

Monitor drift in Prometheus / Grafana:

| Metric | Type | Description |
|---|---|---|
| `vartrack_watcher_poll_total` | Counter | Total poll cycles, labeled by datasource |
| `vartrack_watcher_drift_total` | Counter | Drift events detected |
| `vartrack_watcher_heal_total` | Counter | Self-heal calls triggered |
| `vartrack_watcher_heal_errors_total` | Counter | Self-heal calls that failed |
| `vartrack_watcher_poll_duration_seconds` | Histogram | Poll cycle duration |

---

## Poll interval

Adjust how often the watcher checks each datasource:

```bash
POLL_INTERVAL=30s go run ./watcher-service/cmd
```

Lower values mean faster drift detection but more load on datasources.

---

## Why polling? (and how it's kept efficient)

varTrack uses a **scheduled pull model** rather than event-driven push because it must support eight fundamentally different backends — MongoDB, Redis, ZooKeeper, S3, Kubernetes ConfigMaps, Helm, Vercel, and Linux servers. Each backend exposes a different (or no) change-notification mechanism:

| Datasource | Native change events? |
|---|---|
| MongoDB | Change Streams (replica set only) |
| Redis | Keyspace notifications (off by default, extra config) |
| ZooKeeper | Watches (per-znode, limited concurrency) |
| S3 | Event notifications (requires SNS/SQS wiring) |
| Linux server | None — SSH file stat only |
| Vercel | None |
| ConfigMap | Kubernetes informer (requires in-cluster) |
| Helm | None — release state only |

A unified polling model means a single code path works across all backends with no external infrastructure requirements.

**How the overhead is minimised:**

- **Lightweight reads** — for key/value stores (Redis, MongoDB document), the watcher fetches only the keys declared in the rule's config file, not the entire database.
- **Configurable interval** — default 60 s; set `POLL_INTERVAL` to match your SLA.
- **Leader election** — in multi-replica deployments, only one replica polls at a time (ZooKeeper or Redis distributed lock).
- **Shared state store** — replicas share the baseline via Redis so each poll compares against a consistent snapshot.
- **Circuit breaker** — if a datasource is unreachable, the watcher backs off exponentially rather than hammering it.

---

## Multi-replica deployments

When running multiple watcher replicas, only one should run the heal loop at a time. Leader election ensures this:

```cue
global_tags: {
  watcher_leader_election_datasource: "redis"   // or "zookeeper"
}
```

Replicas that are not the leader still poll and detect drift, but defer healing to the current leader. If the leader crashes, another replica acquires the lock within one TTL window (15 seconds for Redis).

---

## Shared state store

In multi-replica deployments, all replicas can share the same baseline state via Redis:

```cue
global_tags: {
  watcher_state_redis: "redis"   // name of a configured redis datasource
}
```

This prevents replicas from triggering unnecessary heals due to stale local state.
