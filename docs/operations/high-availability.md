# High availability and scaling

Every varTrack service can run as several replicas. This page says what each one shares,
what has to be shared for replicas to work together, and what one replica does alone.

| Service | Replicas | They share | Set |
|---|---|---|---|
| gateway | any number, behind a load balancer | the webhook replay store | `gateway_nonce_datasource` in `config.cue` |
| orchestrator API | any number | Redis (broker, sync ledger), the Git and schema caches | `CELERY_BROKER_URL`, shared `GIT_CACHE_DIR` and `SCHEMA_CACHE_DIR` volumes |
| orchestrator workers | any number; more workers sync more targets at once | the same | the same, `workers.concurrency` in `config.cue` per worker, and how many containers (below) |
| watcher | any number; one leads, the others wait | a lock | `watcher_leader_election_datasource` in `config.cue` |

---

## gateway

The gateway keeps no sync state: it verifies a webhook and hands it to the orchestrator over
gRPC. One thing has to be shared between replicas — the record of deliveries already seen,
which refuses a replayed webhook. Point it at a Redis datasource of the bundle:

```cue
bundle: {
  datasources: [{redis: {tag: "broker", hosts: ["redis:6379"]}}]
  gateway_nonce_datasource: "redis-broker"
}
```

The datasource's `password` may be a string, a `{file: …}` or a `{ref: …}`; any character in
it is fine.

Replay protection never quietly gets weaker:

- **`APP_ENV=production` without `gateway_nonce_datasource`, or with one the gateway cannot
  reach or log in to, does not start.** Each replica would otherwise remember only the
  deliveries it saw itself, and a replay sent to another replica would not be caught.
- **If Redis fails later, a webhook that cannot be checked is refused** with `503`
  (`GW_REPLAY_PROTECTION_UNAVAILABLE`, `Retry-After: 5`) and is not recorded, so the platform's
  retry is the first time it is seen. Readiness reports `nonce_store unavailable` until Redis
  answers.
- **Outside production**, an operator who wants a development gateway with no Redis names the
  mode: `VARTRACK_ALLOW_DEGRADED=nonce_store`. The gateway then keeps deliveries in its own
  process, logs it at startup and lists it under `degraded` in `/health/readiness` for as long as
  it runs. Production refuses the mode whatever is named.

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

## Workers: how many syncs at once

The syncs that can run at once are **the containers times the processes in each**:

- **Processes in a container:** `workers.concurrency` in `config.cue` (default 4, 1 to 64). A machine that needs another number sets `VARTRACK_WORKER_CONCURRENCY` in its environment, which is over `config.cue`. The worker logs where its number came from when it starts. See [`workers`](../configuration/bundle.md#workers).
- **Containers:** this is not set in `config.cue`, because varTrack does not start its own containers. With Docker Compose:

  ```bash
  docker compose up -d --scale orchestrator-worker=3    # three containers, 12 syncs at once with the default 4
  ```

  On Kubernetes it is the worker Deployment's `replicas`, which you write: varTrack ships no chart.

**A limit per store.** `workers.concurrency` and the number of containers say how many syncs run at once in all; [`sync_limits`](../configuration/bundle.md#sync_limits) say how many of them may write to one store (or one server behind several datasources) at once, however many workers there are. The two apply together: a sync that finds its store's slots taken waits a few seconds and then goes back on the queue, so it does not hold a worker process while it waits.

Replicas share Redis, so a task goes to whichever worker is free. Each pool process keeps its own clone of the tenants' schemas and loads them when it starts, so a worker is ready for its first sync as soon as its processes are up.

**What one machine did.** Measured on the e2e stack in Docker Desktop (4 CPUs, 5.8 GiB), with 400 push webhooks sent at once to a Mongo, a Redis and an S3 rule. The median of 3 runs:

| Pool processes | Syncs per second | One sync, in the worker | Waiting in the queue | Worker memory |
|---|---|---|---|---|
| 1 | 11.7 | 59 ms | 1.7 s | 172 MiB |
| 2 | 22.1 | 67 ms | 0.76 s | 268 MiB |
| 4 (the default) | 24.6 | 94 ms | 0.54 s | 466 MiB |
| 8 | 31.1 | 147 ms | 0.36 s | 819 MiB |
| 16 | 32.5 | 294 ms | 0.17 s | 1526 MiB |

- A second process doubled the throughput. After that the gains were small and uneven (4 and 8 processes added 11% and 40% over 2), because the processes share the CPUs and each sync gets slower. 16 processes did no more than 8, with twice the memory.
- A pool process that took 1000 syncs without being replaced stayed at 134 MiB, so `workers.max_tasks_per_child` is a safeguard for stores whose client libraries leak, not something this workload needs.
- Every sync finished in every run.

These are one laptop's numbers and say how the settings compare, not what your hosts will do. To find yours: start at the default, raise `workers.concurrency` while syncs wait in the queue and the machine has CPU and memory to spare, and stop when a single sync's own time starts to rise. `e2e/load/sweep.py` repeats this measurement on the e2e stack.

## Sizing notes

| Knob | Effect |
|---|---|
| `workers.concurrency` (config.cue; `VARTRACK_WORKER_CONCURRENCY`; default 4) | Syncs one worker container runs at once |
| `workers.max_tasks_per_child` (config.cue; `VARTRACK_WORKER_MAX_TASKS_PER_CHILD`; default 200) | Syncs a pool process does before it is replaced; `0` never replaces one |
| `SCHEMA_WARM_TIMEOUT` (10) | Seconds a new pool process waits for the tenants' schemas before it takes work; `0` turns the warm start off |
| `POLL_INTERVAL` (watcher, 60 s) | How often drift is checked; each poll makes the orchestrator read every `self_heal` datasource |
| `sync_mode` | `GIT_SMART_REPAIR` reads the sink before writing and writes only what differs (nothing, when the sink already holds the file); `AUTO` uses it above 500 keys |
| `GIT_CACHE_MAX_ENTRIES` (20) | Mirrors kept on disk per cache directory |
| `SCHEMA_TTL_SECONDS` (300) | How often a tenant's schema repository is refreshed |
