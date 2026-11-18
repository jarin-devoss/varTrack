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
| `.xml` | XML | Elements become dot-separated keys. An attribute is under `@attrs` (`add.@attrs.key`) and the text of an element that has attributes or children under `#text`, so a `web.config` keeps its `key` and `value`. Every value is text. |

A file's **extension** says its format before its name does: `settings.json` is JSON, `database.toml` is TOML and `variables.tf` is HCL, though `settings`, `database` and `variables` on their own stand for YAML. Only a name with no known extension keeps the format its name stands for (`.env.staging`, `application`). A `.tfvars` file is read as `key = "value"` lines (so a string's quotes are not in its value) and is not a format a file sink writes.

Use `--format` on the `vt sync` command to override auto-detection.

!!! note "What a schema sees"
    The *Flattened result* below is what a sink that stores text gets (every value a string). A [CUE schema](schema-registry.md#cue-schemas) is shown each value **as the file wrote it**, next to the other form it can be read as: `database.port` is the int `27017` in a YAML, JSON, TOML or HCL file, and the text `"27017"` in a `.env`, INI or XML file or when quoted. `int`, `bool` and ranges work for both; see [Numbers and booleans](schema-registry.md#numbers-and-booleans).

---

## What a file sink writes

A sink with `update_strategy: STRATEGY_FILE` (S3 and a Linux server) stores each file **as a file**, not as keys.

- **In its own format.** A YAML file is a YAML file in the store, an XML file is XML, and so on for JSON, TOML, HCL, INI, `.env` and `.properties`.
- **As it is in Git when nothing changed it**: byte for byte, with its comments, order, quoting and line endings. A sync changes a file when it takes an [environment out of it](../concepts/environment-resolution.md), extracts a `root_key`, resolves a secret or renames a key (`variables_map`).
- **Rendered in the same format when it did.** The result keeps the source's key order and indentation, but not comments, which a rendered file cannot carry. varTrack reads what it wrote back with that format's own parser: when it would not read as what was meant (a key lost, a value changed), the sync fails and names the key. A file is never written as something that reads differently from Git.
- **Under its own name.** S3 stores it at `<prefix>/<the file's name>`; a Linux server writes it at the rule's path. A name the sink is given says the format by its extension (`file_pattern: "{env}.json"` on S3, a `destination_template` ending in `.yaml` on a server), so a path that ends in `.json` gets JSON whatever the source was.

### Converting a file: `file_format`

A rule's `file_format` (`yaml`, `json`, `toml`, `hcl`, `xml`, `ini`, `env` or `properties`) names the format a FILE sink writes, whatever the source was. The file takes the new format's extension (`app-settings.yaml` with `file_format: json` is `app-settings.json`), unless the sink was given a name. **A file name whose extension says another format than `file_format` is refused when the rule loads**, naming both.

| Into | What it holds | What it refuses (the sync fails, naming the key) |
|---|---|---|
| YAML, JSON | any file | nothing |
| TOML | tables, arrays, numbers, booleans | a `null` (TOML has none) |
| HCL | attributes and objects; strings are written quoted | a key HCL cannot write so that it reads back the same |
| XML | elements and repeated elements; `@attrs` are attributes and `#text` the text of an element that has attributes | a key that is not an XML name (`1st`, `a b`), or a namespaced one |
| INI | sections from the first part of a dotted key | a key outside any section next to sections: INI would read it into every section |
| `.env`, `.properties` | one `key=value` per dotted key | a value on more than one line |

- **XML needs one root element.** A file with several top-level keys is wrapped in `<config>` (`xml_root_tag` names another), and reading it back removes it again, so the keys do not change.
- **A flat file becomes a tree** for a format that holds one: `database.host=x` is `database: {host: x}`, and the keys `items.0`, `items.1` are a list.
- **A source with no types gives strings.** XML, INI, `.env` and `.properties` have only text, so `port=5432` becomes `port: "5432"` in YAML.
- **Drift** compares the stored file with Git's keys. For a file converted into HCL or into XML with the added root, a rule that names its file (`file_name`) removes the quotes and the root before it compares; a rule with a `file_path_map` has no single source format, and reads them as written.
- **HCL comments are not settings.** They are no longer synced as `__comments__` keys.

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
