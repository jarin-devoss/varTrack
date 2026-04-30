# CUE Schema Annotations

varTrack supports two custom annotations in CUE schema files: `@secret()` and `@logger()`. Both are declared on fields in your schema, not in your config files.

---

## `@secret()`

Marks a field whose value is a Vault path at ETL time instead of a real value. Before writing to any sink, the orchestrator reads the schema, finds every `@secret`-annotated field, fetches the real value from Vault, and injects it in-place.

### Syntax

```cue
// default secret manager
"db.password": string @secret()

// named secret manager (matches secret_managers[].vault tag in the bundle)
"api_key": string @secret(ref="other-vault")
```

### Config file side

In the actual config file being synced (YAML, JSON, TOML, etc.) the field value is the Vault path:

```yaml
# configs/app.yaml
database:
  password: "@secret(secret/myapp/db#password)"

auth:
  jwt_secret: "@secret(secret/myapp/auth#jwt_secret)"
```

The format is `@secret(mount/path#field)` — mount path + `#` + field name inside the Vault secret.

### What happens at ETL time

```
1. Parse schema → find @secret-annotated fields
2. For each field present in flat_data:
   a. Determine secret manager: ref= or "default"
   b. Fetch value from Vault: mount/path → field
   c. Replace flat_data[field] with the real value
3. Write to sink — Vault value arrives, not the path string
```

### Dry-run behaviour

In dry-run mode, `@secret` fields are **masked as `***`** in the report. The real value is fetched from Vault but never returned to the caller.

### Bundle wiring

The `ref=` value must match the `tag` of a secret manager declared in the bundle:

```cue
bundle: {
  secret_managers: [
    {
      vault: {
        tag:         "default"   // matched by @secret()
        endpoint:    "https://vault.mycompany.com"
        mount_point: "secret"
        kv_version:  2
        token_auth: { token: "hvs.xxxx" }
      }
    },
    {
      vault: {
        tag:         "other-vault"   // matched by @secret(ref="other-vault")
        endpoint:    "https://vault2.mycompany.com"
        mount_point: "secret"
        kv_version:  2
        token_auth: { token: "hvs.yyyy" }
      }
    },
  ]
}
```

---

## `@logger()`

Marks a field for **structured change logging**. Every time a value changes between ETL runs, the orchestrator emits an INFO log entry showing the field name, old value, new value, datasource, and environment.

### Syntax

```cue
app_port:      int    @logger()
database.host: string @logger()
```

### Log output

When a non-secret field changes:

```
field changed  datasource=mongo  env=production  field=app_port
               from=8080  to=9090  file=configs/app.yaml
```

When a field is written for the first time (no old value):

```
field changed  datasource=mongo  env=production  field=database.host
               to="db.prod.internal"  file=configs/app.yaml
```

### Combining with `@secret()`

You can annotate a field with both. The change is logged but the values are **never revealed** — only `changed=true` is recorded:

```cue
"database.password": string @secret() @logger()
```

```
field changed  datasource=mongo  env=production  field=database.password
               changed=true  file=configs/app.yaml
```

---

## Full schema example

```cue
// schemas/myapp.cue
#Config: {
  app_port:          int & >=1024 & <=65535   @logger()
  log_level:         "debug" | "info" | "warn" | "error"
  "database.host":   string & !=""            @logger()
  "database.port":   int                      @logger()
  "database.password": string & !=""          @secret() @logger()
  "api.secret_key":  string & !=""            @secret()
  max_connections:   int & >=1 & <=1000
}
```

Register the schema in your bundle:

```cue
bundle: {
  schema_registry: {
    platform: "github"
    repo:     "my-org/schemas"
    branch:   "main"
  }
  // ...
}
```

varTrack fetches the schema from Git, runs `cue vet` to validate the payload, then processes `@secret` and `@logger` annotations before writing to any sink.
