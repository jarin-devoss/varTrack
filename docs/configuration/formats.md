# Supported File Formats

varTrack auto-detects the format of your config file from its extension. All formats are parsed into the same flat `{ "key": "value" }` map, which is what is validated and what most sinks write. Sinks that store a file as one document (MongoDB and S3 with `update_strategy: "STRATEGY_DOCUMENT"` or `"STRATEGY_FILE"`) also keep the shape the file parsed to: nested for YAML, JSON, TOML, INI, HCL and XML, flat for `.env`.

---

## Supported formats

| Extension | Format | Notes |
|---|---|---|
| `.yaml`, `.yml` | YAML | Nested keys flattened with dot notation. Numbers and booleans keep their types. |
| `.json` | JSON | Nested keys flattened with dot notation. Numbers and booleans keep their types. |
| `.toml` | TOML | Sections become key prefixes. Numbers and booleans keep their types. |
| `.env` | dotenv / KEY=VALUE | One `KEY=value` per line. Every value is text. |
| `.ini` | INI | Section + key become `section.key`. Every value is text. |
| `.hcl` | HCL | Terraform-style config files. Numbers and booleans keep their types. |
| `.xml` | XML | Elements become dot-separated keys. Every value is text. |

Use `--format` on the `vt sync` command to override auto-detection.

!!! note "What a schema sees"
    The *Flattened result* below is what a sink that stores text gets (every value a string). A [CUE schema](schema-registry.md#cue-schemas) is shown each value **as the file wrote it**, next to the other form it can be read as: `database.port` is the int `27017` in a YAML, JSON, TOML or HCL file, and the text `"27017"` in a `.env`, INI or XML file or when quoted. `int`, `bool` and ranges work for both; see [Numbers and booleans](schema-registry.md#numbers-and-booleans).

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

A config file can hold the settings of every environment, and varTrack reads out the one being synced. **Which names are environments is what the rule declares**: the values of `branch_map`, the keys of `file_path_map`, every branch with `branch_as_env`, and so on, plus `default` as the fallback. The rules, and every shape with its result, are in [Several environments in one file](../concepts/environment-resolution.md#several-environments-in-one-file). In short, there are two shapes, and they combine.

Environment objects at the root, with the settings every environment shares next to them:

```yaml
# configs/app.yaml   (rule: branch_map {main: production, develop: staging})
max_connections: 50          # no environment: every environment gets it

production:
  database_host: mongo.prod.internal
  log_level: warn
staging:
  database_host: mongo.staging.internal
default:
  database_host: mongo.local
  log_level: debug
```

Push to `main` (env = `production`):

```
database_host   = "mongo.prod.internal"
log_level       = "warn"
max_connections = "50"
```

Push from PR #42 (env = `pr-42`, which the file does not name, so `default`):

```
database_host   = "mongo.local"
log_level       = "debug"
max_connections = "50"
```

One value per environment, where only some keys differ:

```yaml
database_host:
  production: mongo.prod.internal
  staging:    mongo.staging.internal
  default:    mongo.local
max_connections: 50          # the same for every environment
```

An environment's settings override `default`'s key by key, at every depth. A file with neither the environment nor a `default` has nothing for that environment: it is skipped, or with `strict_validation` it fails, and another environment's settings are never written under it.

### Combining with `root_key`

Both shapes work after `root_key` extraction, so the settings can sit inside a file owned by other tooling:

```yaml
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
  root_key:  "vartrack"      // extract the vartrack block first, then read the environments
  branch_map: { main: "production", develop: "staging" }
}]
```

Push to `main` → extracts the `vartrack` subtree → writes `API_URL=https://api.prod.com`, `LOG_LEVEL=warn`, `TIMEOUT=30`.
