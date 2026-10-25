# Glossary

The words varTrack's docs, logs and web UI use, and what each one means here.

**Platform**
:   A Git server that sends varTrack push webhooks: an entry of `platforms` in `config.cue`
    (`github`, `gitea`, or `{type}-{tag}` such as `gitea-team`). It also answers who a
    caller is and what they may do, for the CLI API and the web UI.

**Datasource**
:   A place varTrack writes configuration to: an entry of `datasources` in `config.cue`
    (`mongo`, `redis`, `s3-minio`, …). Its block holds the connection settings and
    credentials. Each datasource type is written by a **sink**.

**Sink**
:   The code that writes one datasource type — MongoDB, Redis, ZooKeeper, S3, Kubernetes
    ConfigMap, Helm, Vercel, Linux server. See [Sinks](../sinks/index.md).

**Rule**
:   What is synced, from where, to where and how: one entry of `rules` in `config.cue`,
    named by its **platform** and **datasource** (at most one rule per pair). A tenant's
    `bundle.json` and a repository's `vartrack.json` can adjust it. See
    [How a rule is built](../configuration/index.md#how-a-rule-is-built).

**Layer**
:   One of the files a rule is built from — `config.cue`, `bundle.json`, `vartrack.json` —
    plus `config.cue`'s `force`, applied last. Each overrides the one before.

**Tenant**
:   A team with its own **schema repository**, registered with `SCHEMA_TENANT_<ID>_REPO`.
    Its `bundle.json` holds its layer of the rules, its CUE schemas validate its files, and
    the web UI shows each user the tenants' rules their Git account can read.
    A rule no tenant's `bundle.json` names belongs to tenant `default`.

**Schema repository** (schema registry)
:   A tenant's Git repository with `bundle.json` and the CUE schemas. See
    [Schema registry](../configuration/schema-registry.md).

**Config repository**
:   A repository whose pushes a rule syncs: it holds the configuration files
    (`configs/app.yaml`, …) and, optionally, a `vartrack.json`.

**Preset**
:   A JSON file of rule settings in any repository, pulled into a `bundle.json` or a
    `vartrack.json` with `extends`. See [Presets](../configuration/schema-registry.md#presets-extends).

**Environment**
:   The label a push is synced under — `production`, `staging`, `pr-42`, `v1.2.0` — from the
    rule's `branch_map`, or the branch, PR number or tag. See
    [Environment resolution](environment-resolution.md).

**Target**
:   One environment of one datasource, for one tenant: the unit varTrack records state for,
    locks while writing, prunes and checks for drift. The web UI's **Environments** page
    lists targets.

**Desired state**
:   What Git says a target holds: the flat keys of the files last synced to it, kept per file
    in the sync ledger (Redis). Secrets are kept as digests, never as values.

**Flat key**
:   A value's path in its file, dot-separated: `database.host`, `api.regions.0`. Every file
    format is read as flat keys; a sink stores them as they are, or nested again (MongoDB's
    document strategy, the JSON structure of Redis).

**Key space**
:   Where in a store a target's keys land — a Redis hash or prefix, a ZooKeeper path, a
    Vercel target. Two targets with the same key space write the same keys; prune never
    deletes a key another target still holds there.

**Sync**
:   One run that reads the pushed files at the pushed commit, validates them, and writes them
    to the datasource: started by a push, by `vt sync`, by the web UI, or by self-heal. Each
    sync is a **task**.

**Prune**
:   Deleting keys varTrack wrote before that Git no longer holds, once per environment after
    its files are written. Only on sinks that keep each key separately. See
    [What prune deletes](sync-strategies.md#what-prune-deletes).

**Drift**
:   A key whose value in the datasource differs from the desired state, or is missing.
    See [Drift detection](drift-detection.md).

**Self-heal**
:   The watcher asking the orchestrator, on every poll, whether each `self_heal` rule's
    datasource still matches Git, and having it write Git's values back when it does not.

**Dry run**
:   A sync that reads, validates and reports everything but writes nothing.

**Shape**
:   How a MongoDB datasource stores a file: one document per key (`STRATEGY_KEY_VALUE`) or one
    document per file (`STRATEGY_DOCUMENT`, `STRATEGY_FILE`). Changing it replaces the other
    shape's documents and records a **Shape changed** event.
