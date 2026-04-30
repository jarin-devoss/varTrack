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

```
Every POLL_INTERVAL (default: 60s)
  │
  ├── Read live values from datasource
  ├── Compare against Git-sourced baseline (key by key)
  │
  ├── No diff   → log "OK", update poll counter
  │
  └── Diff found
        ├── Increment vartrack_watcher_drift_total counter
        ├── Log the drifted keys and their values
        └── self_heal: true?
              ├── Yes → call TriggerSync gRPC → orchestrator re-runs ETL pipeline
              └── No  → log drift, alert via metrics, do nothing
```

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

1. Watcher reads `max_connections = 5` from MongoDB
2. Compares against baseline: expected `50`, got `5`
3. `self_heal: true` → calls `TriggerSync` on orchestrator
4. Orchestrator fetches the file from Git, parses it, writes `max_connections = 50` back to MongoDB
5. Next poll sees no drift — state restored

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
