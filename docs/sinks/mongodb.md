# MongoDB Sink

varTrack writes config values to MongoDB as key-value documents. Three write strategies are supported: **key-value** (one document per key), **document** (one document containing all keys), and **file** (one document per file containing all keys as a nested map).

---

## Configuration

```cue
datasources: [{
  mongo: {
    tag:             ""                         // optional — resolves to "mongo-{tag}"
    endpoint:        "mongodb://mongo:27017"
    database:        "vartrack"
    collection:      "variables"
    update_strategy: "STRATEGY_KEY_VALUE"       // or "STRATEGY_DOCUMENT"
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

### `STRATEGY_KEY_VALUE` (default)

Each config key is stored as a separate document:

```json
{ "key": "database.host",     "value": "mongo.prod.internal" }
{ "key": "max_connections",   "value": "50" }
{ "key": "feature.dark_mode", "value": "true" }
```

Reading a value: `db.collection.findOne({ key: "database.host" }).value`

### `STRATEGY_DOCUMENT`

All config keys are stored in a single document:

```json
{
  "database.host":     "mongo.prod.internal",
  "max_connections":   "50",
  "feature.dark_mode": "true"
}
```

Reading a value: `db.collection.findOne({})["database.host"]`

### `STRATEGY_FILE`

The entire key set is stored as a single document with a nested `data` map, one document per synced file:

```json
{
  "_vt_env":       "production",
  "_vt_file_path": "configs/app.yaml",
  "data": {
    "database.host":   "mongo.prod.internal",
    "max_connections": "50"
  }
}
```

Efficient for large key sets since it only touches one document per sync.

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

The watcher-service reads all documents from the configured collection and compares `key → value` pairs against the Git baseline. Any added, changed, or deleted key triggers a drift event.

Enable self-heal to restore automatically:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  self_heal:  true
}]
```
