# High availability and scaling

Every varTrack service can run as several replicas. This page says what each one shares,
what has to be shared for replicas to work together, and what one replica does alone.

| Service | Replicas | They share | Set |
|---|---|---|---|
| gateway | any number, behind a load balancer | the webhook replay store | `gateway_nonce_datasource` in `config.cue` |
| orchestrator API | any number | Redis (broker, sync ledger), the Git and schema caches | `CELERY_BROKER_URL`, shared `GIT_CACHE_DIR` and `SCHEMA_CACHE_DIR` volumes |
| orchestrator workers | any number; more workers sync more targets at once | the same | the same, and `--concurrency` per worker |
| watcher | any number; one leads, the others wait | a lock | `watcher_leader_election_datasource` in `config.cue` |

---

## gateway

The gateway keeps no sync state: it verifies a webhook and hands it to the orchestrator over
gRPC. One thing has to be shared between replicas — the record of deliveries already seen,
which refuses a replayed webhook. Point it at a Redis datasource of the bundle:

```cue
bundle: {
  datasources: [{redis: {tag: "broker", host: "redis", port: 6379}}]
  gateway_nonce_datasource: "redis-broker"
}
```

Without it each replica remembers the deliveries it saw itself, so a replay sent to another
replica is not caught. If Redis cannot be reached at startup the gateway logs
`Redis nonce store unavailable — using in-process memory store`; if it fails later, a
delivery is let through rather than all traffic blocked.

The rate limiter counts per replica, so the limit a client meets grows with the number of
replicas.

## orchestrator

The API receives webhooks and CLI requests over gRPC and queues a task; Celery workers run
the syncs. Scale the workers for throughput.

**One sync per target at a time.** A sync takes a lock in Redis for every environment it
writes (`vt:sync-lock:<tenant>:<datasource>:<env>`), in a fixed order so two syncs never wait
on each other. A second sync of the same environment waits up to 300 s, then fails with
`another sync of <datasource>/<env> is still running`. The lock is a lease of 600 s — the
task time limit — so a worker that dies does not hold an environment for good.

**Share the caches.** Mount the same volume at `GIT_CACHE_DIR` and at `SCHEMA_CACHE_DIR` in
the API and in every worker on a host:

- `GIT_CACHE_DIR` holds one bare mirror per config repository. Clones and fetches of a
  repository are locked across the processes that share the directory (on Linux), and files
  are read by commit, so readers never see a half-updated mirror. At most
  `GIT_CACHE_MAX_ENTRIES` mirrors are kept; the least recently used go first, never one in use.
- `SCHEMA_CACHE_DIR` holds the tenants' schema repositories; a worker reads the clone the API
  made instead of cloning again.

Each host can have its own volume; a mirror is a cache and is cloned again where it is missing.

**What lives in Redis** (the one at `CELERY_BROKER_URL`). The Celery broker and results, the sync ledger (what each target
should hold, per file), the web UI's tasks and events, and the locks. Losing it loses that
history and the inventory prune works from: the next sync of a target prunes nothing, as on a
first sync. Run Redis with persistence (AOF) in production.

## watcher

Every replica asks the orchestrator for the rules to watch, but only the leader polls and
heals; the others stay idle until it stops. Leader election needs a lock in Redis or
ZooKeeper:

```cue
bundle: {
  watcher_leader_election_datasource: "redis"   // or "zookeeper", or a tagged one
}
```

If the leader stops, another replica takes over within one lock period (15 s with Redis).
The watcher keeps no state of its own, so a new leader starts where the last one stopped.

---

## Sizing notes

| Knob | Effect |
|---|---|
| Worker `--concurrency` (`WORKER_CONCURRENCY` in the compose file, 4) | Syncs one worker runs at once |
| `POLL_INTERVAL` (watcher, 60 s) | How often drift is checked; each poll makes the orchestrator read every `self_heal` datasource |
| `max_changed_files` (rule, 500) | The most files one push syncs |
| `sync_mode` | `GIT_SMART_REPAIR` reads MongoDB before writing and writes only what changed; `AUTO` uses it above 500 keys |
| `GIT_CACHE_MAX_ENTRIES` (20) | Mirrors kept on disk per cache directory |
| `SCHEMA_TTL_SECONDS` (300) | How often a tenant's schema repository is refreshed |
