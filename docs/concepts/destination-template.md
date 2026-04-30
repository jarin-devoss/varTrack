# Destination Template

`destination_template` is a rule-level field that controls exactly where data lands in each sink — the collection name, key prefix, znode path, file path, or release name — using placeholders resolved at sync time.

---

## Placeholders

| Placeholder | Resolved from |
|---|---|
| `{env}` | Resolved environment (e.g. `production`, `staging`, `pr-42`) |
| `{tenant}` | Tenant ID from the `X-Tenant-ID` request header |

---

## Per-sink behavior

| Sink | What it controls | Template | Resolved value |
|---|---|---|---|
| MongoDB | Collection name | `"{env}-config"` | `production-config` |
| ZooKeeper | znode root path | `"/{tenant}/{env}"` | `/acme/pr-42` |
| Redis | Key prefix | `"{env}:cfg"` | `production:cfg` |
| S3 | Key prefix | `"{tenant}/{env}/"` | `acme/production/` |
| Linux server | Full remote file path | `"/etc/app/{env}.env"` | `/etc/app/production.env` |
| Helm | Release name | `"app-{env}"` | `app-production` |
| ConfigMap | ConfigMap name | `"myapp-{env}"` | `myapp-production` |
| Vercel | Environment variable prefix | `"{env}_"` | `production_` |

---

## Examples

### One template, three environments

```cue
rules: [{
  platform:             "github"
  datasource:           "mongo"
  file_name:            "configs/app.yaml"
  repositories:         ["my-org/*"]
  destination_template: "{env}-config"
  branch_map: {
    main:    "production"
    develop: "staging"
    test:    "test"
  }
}]
```

| Push branch | Resolved env | Collection written to |
|---|---|---|
| `main` | `production` | `production-config` |
| `develop` | `staging` | `staging-config` |
| `test` | `test` | `test-config` |

---

### Per-tenant isolation

```cue
destination_template: "{tenant}/{env}-config"
```

A push from tenant `acme` to `main` writes to `acme/production-config`.
A push from tenant `globex` to `main` writes to `globex/production-config`.

---

### PR preview environments

```cue
rules: [{
  platform:             "github"
  datasource:           "mongo"
  file_name:            "configs/app.yaml"
  repositories:         ["my-org/app"]
  destination_template: "{env}-config"
  env_as_pr:            true    // PR #42 → env = "pr-42"
}]
```

Each pull request gets its own isolated collection: `pr-42-config`, `pr-103-config`, etc.

---

### ZooKeeper path per tenant and environment

```cue
rules: [{
  platform:             "github"
  datasource:           "zookeeper"
  destination_template: "/{tenant}/{env}"
}]
```

Writes znodes under `/acme/production/`, `/acme/staging/`, etc.

---

## Validation

If `{env}` appears in the template, at least one environment resolution strategy must be enabled:

- `env_as_branch: true`
- `env_as_pr: true`
- `env_as_tags: true`
- `branch_map: { ... }`
- `file_path_map: { ... }`

varTrack will reject the bundle at startup if `{env}` is used with no resolution strategy configured.

---

## Fallback behavior

If `destination_template` is not set, each sink falls back to its own sink-specific configuration key (e.g. `collection` for MongoDB, `key_prefix` for Redis).
