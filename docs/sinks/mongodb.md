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

### With authentication

```cue
datasources: [{
  mongo: {
    endpoint:  "mongodb://mongo:27017"
    database:  "vartrack"
    username:  "vartrack"
    password:  "secret"                        // or a Vault reference
  }
}]
```

### With TLS

```cue
datasources: [{
  mongo: {
    endpoint:    "mongodb://mongo:27017"
    database:    "vartrack"
    tls_enabled: true
    tls_ca_file: "/etc/ssl/mongo-ca.pem"
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

### `STRATEGY_KEY_VALUE`

Each config key is stored as a separate document, its value as a string:

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
  self_heal:  true
}]
```
