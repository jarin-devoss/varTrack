# Supported File Formats

varTrack auto-detects the format of your config file from its extension. All formats are parsed into the same flat `{ "key": "value" }` map before writing to any sink.

---

## Supported formats

| Extension | Format | Notes |
|---|---|---|
| `.yaml`, `.yml` | YAML | Nested keys flattened with dot notation |
| `.json` | JSON | Nested keys flattened with dot notation |
| `.toml` | TOML | Sections become key prefixes |
| `.env` | dotenv / KEY=VALUE | One `KEY=value` per line |
| `.ini` | INI | Section + key become `section.key` |
| `.hcl` | HCL | Terraform-style config files |
| `.xml` | XML | Elements become dot-separated keys |

Use `--format` on the `vt sync` command to override auto-detection.

---

## Examples

=== "YAML"

    ```yaml
    # configs/app.yaml
    database:
      host: mongo.prod.internal
      port: 27017
      name: myapp

    feature_flags:
      dark_mode: true
      beta_ui: false

    max_connections: 50
    ```

    Flattened result:

    ```
    database.host         = "mongo.prod.internal"
    database.port         = "27017"
    database.name         = "myapp"
    feature_flags.dark_mode = "true"
    feature_flags.beta_ui   = "false"
    max_connections         = "50"
    ```

=== "JSON"

    ```json
    {
      "database": {
        "host": "mongo.prod.internal",
        "port": 27017
      },
      "max_connections": 50
    }
    ```

    Flattened result:

    ```
    database.host    = "mongo.prod.internal"
    database.port    = "27017"
    max_connections  = "50"
    ```

=== ".env"

    ```bash
    # configs/app.env
    DATABASE_HOST=mongo.prod.internal
    DATABASE_PORT=27017
    MAX_CONNECTIONS=50
    FEATURE_DARK_MODE=true
    ```

    Flattened result (keys preserved as-is):

    ```
    DATABASE_HOST       = "mongo.prod.internal"
    DATABASE_PORT       = "27017"
    MAX_CONNECTIONS     = "50"
    FEATURE_DARK_MODE   = "true"
    ```

=== "TOML"

    ```toml
    [database]
    host = "mongo.prod.internal"
    port = 27017

    [feature_flags]
    dark_mode = true
    ```

    Flattened result:

    ```
    database.host          = "mongo.prod.internal"
    database.port          = "27017"
    feature_flags.dark_mode = "true"
    ```

=== "INI"

    ```ini
    [database]
    host = mongo.prod.internal
    port = 27017

    [app]
    max_connections = 50
    ```

    Flattened result:

    ```
    database.host    = "mongo.prod.internal"
    database.port    = "27017"
    app.max_connections = "50"
    ```

=== "HCL"

    ```hcl
    database {
      host = "mongo.prod.internal"
      port = 27017
    }

    max_connections = 50
    ```

    Flattened result:

    ```
    database.host    = "mongo.prod.internal"
    database.port    = "27017"
    max_connections  = "50"
    ```

---

## Root key slicing

If your config file uses a `vartrack` top-level key, varTrack extracts only that subtree:

```yaml
# Other top-level keys are ignored
app_name: my-service
version: 2.3.1

vartrack:
  database_host: mongo.prod.internal
  max_connections: 50
```

Only `database_host` and `max_connections` are written. This lets you keep varTrack config alongside other metadata in the same file.

---

## Dry-run — preview what would be written

Before any real sync, you can simulate the full parse → flatten → env-slice pipeline without writing anything to a sink:

```bash
# CLI
vt sync --file configs/app.yaml --datasource mongo --env staging --dry-run

# HTTP
POST /v1/webhooks/mongo/dry-run
```

The response shows the exact flat key/value map that would be written — after root_key extraction, env-slicing, and `@secret` masking. Nothing touches the datasource.

This is especially useful when introducing a new file format or env pattern: run dry-run first to confirm varTrack is parsing and slicing the file exactly as expected before merging to `main`.

---

## Environment key patterns

varTrack auto-detects three patterns for environment-keyed config files. No rule config is needed — the structure is inferred automatically.

---

### Pattern 1 — all top-level keys are env names

Every top-level value is a dict. varTrack merges `default` first, then applies the matching env as an override.

```yaml
# configs/app.yaml
production:
  database_host: mongo.prod.internal
  max_connections: 100
  log_level: warn

staging:
  database_host: mongo.staging.internal
  max_connections: 10
  log_level: info

default:
  max_connections: 5      # fallback for any unknown env
  log_level: debug
  cache_ttl: 60
```

Push to `main` (resolved env = `production`):

```
database_host   = "mongo.prod.internal"
max_connections = "100"
log_level       = "warn"
cache_ttl       = "60"           ← inherited from default
```

Push from PR #42 (env = `pr-42`, no match → `default`):

```
max_connections = "5"
log_level       = "debug"
cache_ttl       = "60"
```

---

### Pattern 2 — per-field env sub-keys mixed with scalars

Each field that varies per env has a dict of `env → value`. Fields that are the same across all envs are plain scalars — they are written as-is for every env.

```yaml
# configs/app.yaml
database_host:
  production: mongo.prod.internal
  staging:    mongo.staging.internal
  default:    mongo.local

max_connections: 50        # same for every env
log_level: info            # same for every env
cache_ttl:
  production: 300
  staging:    60
  default:    10
```

Push to `main` (env = `production`):

```
database_host   = "mongo.prod.internal"
max_connections = "50"
log_level       = "info"
cache_ttl       = "300"
```

Push to `develop` (env = `staging`):

```
database_host   = "mongo.staging.internal"
max_connections = "50"
log_level       = "info"
cache_ttl       = "60"
```

---

### Pattern 3 — env dicts and scalar defaults at the same level

Some keys at the same nesting level are env names (dicts with overrides), others are plain scalars that apply to every env. Order in the file doesn't matter — varTrack categorizes by value type, not position. The env dict is merged on top of the scalars.

```json
{
  "production": { "database_host": "mongo.prod.internal", "log_level": "warn"  },
  "staging":    { "database_host": "mongo.stg.internal",  "log_level": "info"  },
  "default":    { "database_host": "mongo.local",         "log_level": "debug" },
  "max_connections": 50,
  "cache_ttl":       60,
  "app_name":        "my-service"
}
```

Push to `main` (env = `production`):

```
database_host   = "mongo.prod.internal"   ← from production dict
log_level       = "warn"                  ← from production dict
max_connections = "50"                    ← from scalar
cache_ttl       = "60"                    ← from scalar
app_name        = "my-service"            ← from scalar
```

Push from PR #42 (env = `pr-42`, no match → `default` dict):

```
database_host   = "mongo.local"
log_level       = "debug"
max_connections = "50"
cache_ttl       = "60"
app_name        = "my-service"
```

If there is no `default` dict and no matching env dict, data is returned unchanged.

---

### Combining patterns with `root_key`

All three patterns work after `root_key` extraction. This lets you embed varTrack config inside files owned by other tools:

```yaml
# package.json style — but in YAML for readability
name: my-app
version: 1.0.0

vartrack:
  production: { API_URL: "https://api.prod.com", LOG_LEVEL: warn  }
  staging:    { API_URL: "https://api.stg.com",  LOG_LEVEL: info  }
  default:    { API_URL: "https://api.local.com", LOG_LEVEL: debug }
  TIMEOUT: 30
```

```cue
rules: [{
  file_name: "config.yaml"
  root_key:  "vartrack"      // extract the vartrack block first, then detect pattern
  branch_map: { main: "production", develop: "staging" }
}]
```

Push to `main` → extracts `vartrack` subtree → detects Pattern 3 → writes `API_URL=https://api.prod.com`, `LOG_LEVEL=warn`, `TIMEOUT=30`.
