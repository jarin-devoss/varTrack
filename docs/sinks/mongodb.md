# MongoDB Sink

varTrack writes config values to MongoDB. The datasource's `update_strategy` picks the shape: **document** (one nested document per file, the default), **key-value** (one document per key), or **file** (one document per file, in the shape the file parsed to).

---

## Configuration

```cue
datasources: [{
  mongo: {
    tag:             ""                         // optional — resolves to "mongo-{tag}"
    endpoint:        "mongodb://mongo:27017"
    database:        "vartrack"
    collection:      "variables"
    update_strategy: "STRATEGY_DOCUMENT"        // the default; or "STRATEGY_KEY_VALUE", "STRATEGY_FILE"
  }
}]
```

`connect_timeout_ms` (default `0`: the driver's own waits) is how long the sink looks for the server before a write or a read fails. Set it (`5000` is a sensible value): with a stopped server the driver otherwise waits 20 s to connect and 30 s to find a server, more than once per operation, so the web UI keeps saying "in sync" for a minute before it says it cannot read the sink.

### With authentication

```cue
datasources: [{
  mongo: {
    endpoint:   "mongodb://mongo:27017"
    database:   "vartrack"
    collection: "variables"
    username:   "vartrack"
    password:   "secret"                       // or a Vault reference
  }
}]
```

### With TLS

```cue
datasources: [{
  mongo: {
    endpoint:    "mongodb://mongo:27017"
    database:    "vartrack"
    collection:  "variables"
    ssl:         true
    ssl_ca_path: "/etc/ssl/mongo-ca.pem"
  }
}]
```

---

## Write strategies

The examples below sync this file:

```yaml
# configs/app.yaml
database:
  host: mongo.prod.internal
max_connections: 50
regions: [eu, us]
```

!!! info "In the demo"
    The demo's `mongo`, `mongo-primary` and `mongo-dr` datasources use `STRATEGY_DOCUMENT` (collections `variables`, `primary_vars`, `dr_vars`),
    `mongo-document` uses it with its own collection, `mongo-file` uses `STRATEGY_FILE`, and **`mongo-kv`** (collection `kv_vars`) is the one that
    uses `STRATEGY_KEY_VALUE`: the shape that keeps single keys, so it is where prune and per-key drift show.

### `STRATEGY_KEY_VALUE`

Each config key is stored as a separate document, its value as a string. It is the strategy that keeps a key on its own, so it is the one a
[prune](../concepts/sync-strategies.md) can delete from (a document strategy replaces the whole file's document on every sync):

```json
{ "_vt_env": "production", "key": "database.host",   "value": "mongo.prod.internal" }
{ "_vt_env": "production", "key": "max_connections", "value": "50" }
{ "_vt_env": "production", "key": "regions.0",       "value": "eu" }
```

Reading a value: `db.collection.findOne({ _vt_env: "production", key: "database.host" }).value`

### `STRATEGY_DOCUMENT` (default)

One document per environment and file, with the config always nested under `data`. Values keep the types and arrays they have in the file:

```json
{
  "_vt_env":  "production",
  "_vt_file": "configs/app.yaml",
  "data": {
    "database":        { "host": "mongo.prod.internal" },
    "max_connections": 50,
    "regions":         ["eu", "us"]
  }
}
```

Reading a value: `db.collection.findOne({ _vt_env: "production" }).data.database.host`

A flat file (`.env`, `.properties`) is nested by the dots in its keys: `db.host=x` is stored as `data: { db: { host: "x" } }`. A file where one key is both a value and the parent of other keys (`log=on` next to `log.level=info`) has no nested form, so its sync fails and names the key. Use `STRATEGY_FILE` for such a file.

### `STRATEGY_FILE`

One document per environment and file, with `data` in the shape the file parsed to: nested for YAML, JSON, TOML and the other tree formats (as in `STRATEGY_DOCUMENT`), flat for `.env` and `.properties`:

```json
{
  "_vt_env":  "production",
  "_vt_file": "configs/app.properties",
  "data": {
    "database.host":   "mongo.prod.internal",
    "max_connections": "50"
  }
}
```

With either document strategy, every sync replaces the file's document, so keys removed from Git disappear without `prune`. Switching a datasource from `STRATEGY_KEY_VALUE` to one of them removes that file's per-key documents on the next sync; switching back to `STRATEGY_KEY_VALUE` removes the file's document the same way. The orchestrator logs a warning with the number of documents removed, and the UI shows a **Shape changed** event on the environment.

!!! note "The default changed"
    A Mongo datasource that sets no `update_strategy` used to store one document per key. It now stores one nested document per file. To keep per-key documents, set `update_strategy: "STRATEGY_KEY_VALUE"`; without it, the next sync replaces the per-key documents of each file with its document, logs how many it removed and shows a **Shape changed** event.

---

## One write, one step

`STRATEGY_DOCUMENT` and `STRATEGY_FILE` write a file as its document plus the removal of the documents of another shape it replaces (after an `update_strategy` change). On a **replica set or a sharded cluster** those two operations are one session transaction, so a failure leaves the file as it was. Transactions need MongoDB 4.4 or later (an `env_as_collection` collection is created inside the transaction).

A **standalone** server has no transactions: varTrack asks the server once, on the first write, and logs that it falls back. There the document is replaced in one `replace_one`, and a sync that fails after writing a file puts it back from the snapshot it took before (see [A sync failed](../operations/troubleshooting.md#a-sync-failed)). `STRATEGY_KEY_VALUE` writes one document per key with one `bulk_write` and is not wrapped in a transaction: it is not atomic across keys.

---

## Per-environment collections

Set `env_as_collection: true` to write each environment into its own collection instead of a fixed one:

```cue
datasources: [{
  mongo: {
    database:          "vartrack"
    collection:        "configs"
    env_as_collection: true   // production → "configs_production", pr-42 → "configs_pr-42"
  }
}]
```

---

## Destination template

The `destination_template` field controls which collection data is written to:

```cue
rules: [{
  platform:             "github"
  datasource:           "mongo"
  file_name:           "app-settings.yaml"
  destination_template: "{env}-config"
  branch_map: {
    main:    "production"
    develop: "staging"
  }
}]
```

| Push branch | Collection written |
|---|---|
| `main` | `production-config` |
| `develop` | `staging-config` |
| PR #42 | `pr-42-config` |

---

## Drift detection

The watcher asks the orchestrator each poll; the orchestrator reads the keys varTrack wrote — the `key → value` documents, or the keys inside `data` and compares them with what it last synced from Git — a changed or missing key is drift ([Drift Detection](../concepts/drift-detection.md)).

Enable self-heal to restore automatically:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  file_name: "app-settings.yaml"
  branch_map: {main: "production"}
  self_heal:  true
}]
```
