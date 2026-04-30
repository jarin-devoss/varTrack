# Schema Registry

The schema registry is a Git repository that holds two things:

1. **CUE schema files** — used to validate config files before any write
2. **`bundle.json` / `rules.json`** — a rule manifest that lets Celery workers resolve rules locally without a round-trip to the API process

---

## Repo structure

```
my-org/schemas/                  ← schema registry repo
├── bundle.json                  ← rule manifest (optional but recommended)
├── app-settings.yaml.cue        ← CUE schema for app-settings.yaml
├── config.toml.cue              ← CUE schema for config.toml
├── dotenv.cue                   ← CUE schema for .env files
└── default.cue                  ← fallback schema when no file-specific match
```

The schema repo is pointed to from `config.cue`:

```cue
schema_registry: {
  platform: "github"
  repo:     "my-org/schemas"
  branch:   "main"
}
```

varTrack clones this repo for each tenant on first use and refreshes it automatically on a TTL (default 5 minutes) or immediately when the schema repo itself receives a push webhook.

---

## `bundle.json`

`bundle.json` is the primary rule manifest. It is a JSON array of rule objects — one entry per platform + datasource combination the tenant uses.

Workers parse it on startup to resolve `rule_config` locally, avoiding a round-trip to the orchestrator API process on every task.

### Format

Two formats are accepted:

**Array (recommended):**
```json
[
  { ... rule object ... },
  { ... rule object ... }
]
```

**Object with a `rules` key:**
```json
{
  "rules": [
    { ... rule object ... }
  ]
}
```

### Full example

```json
[
  {
    "platform":         "github",
    "datasource":       "mongo",
    "tenant_id":        "acme",
    "repo_url":         "https://github.com/acme/app-configs.git",
    "file_name":        "configs/app.yaml",
    "repositories":     ["acme/*"],
    "branch":           "main",
    "branch_map": {
      "main":    "production",
      "develop": "staging"
    },
    "sync_mode":        "SYNC_MODE_FULL",
    "prune":            true,
    "prune_last":       false,
    "self_heal":        true,
    "root_key":         "vartrack",
    "mongo_uri":        "mongodb://mongo:27017",
    "database":         "vartrack",
    "collection":       "variables",
    "update_strategy":  "STRATEGY_KEY_VALUE"
  },
  {
    "platform":         "github",
    "datasource":       "redis",
    "tenant_id":        "acme",
    "repo_url":         "https://github.com/acme/app-configs.git",
    "file_name":        "configs/app.yaml",
    "repositories":     ["acme/*"],
    "branch":           "main",
    "sync_mode":        "SYNC_MODE_FULL",
    "prune":            true,
    "self_heal":        true,
    "redis_host":       "redis",
    "redis_port":       6379,
    "redis_db":         0
  }
]
```

### Rule object fields

| Field | Required | Description |
|---|---|---|
| `platform` | Yes | Platform name — `github`, `gitlab`, `gitea`, or `github-{tag}` |
| `datasource` | Yes | Datasource name — `mongo`, `redis`, `zookeeper`, `s3`, etc. |
| `tenant_id` | Yes | Tenant this rule belongs to |
| `repo_url` | Yes | HTTPS clone URL of the config repository |
| `file_name` | One of | Single file path to track in all repos |
| `file_path_map` | One of | `{ env → file_path }` for per-environment file selection |
| `repositories` | No | Glob patterns for which repos trigger this rule |
| `branch` | No | Branch to track (default: `main`) |
| `branch_map` | No | `{ branch → env }` label mapping |
| `sync_mode` | No | `AUTO`, `SYNC_MODE_FULL`, `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, `LIVE_STATE` |
| `prune` | No | Delete datasource keys no longer in Git (default: `false`) |
| `prune_last` | No | Defer key deletion until all sources are processed |
| `self_heal` | No | Auto-restore on drift (default: `false`) |
| `root_key` | No | Subtree key to extract before flattening (default: `"vartrack"`) |
| `env_as_branch` | No | Use branch name as the environment label |
| `env_as_pr` | No | Use `pr-{number}` as the environment label |
| `env_as_tags` | No | Use tag name as the environment label |

Additional sink-specific fields (connection strings, credentials, etc.) are included inline per rule — see the individual [sink pages](../sinks/index.md) for what each datasource type accepts.

---

## `rules.json`

`rules.json` is a drop-in alias for `bundle.json`. If both files exist, `bundle.json` takes precedence. Use `rules.json` if you prefer a shorter name or want to distinguish the manifest from other bundle exports.

```
my-org/schemas/
├── rules.json     ← identical format to bundle.json
└── *.cue
```

---

## Tenant configuration

Tenants are registered via environment variables on the orchestrator-service and its Celery workers:

```bash
SCHEMA_TENANT_ACME_REPO=https://github.com/acme/schemas.git
SCHEMA_TENANT_ACME_BRANCH=main
SCHEMA_TENANT_ACME_TOKEN=ghp_xxx
```

The tenant ID is derived from the variable name (`ACME` → `acme`). Multiple tenants can be registered in the same process:

```bash
SCHEMA_TENANT_ACME_REPO=https://github.com/acme/schemas.git
SCHEMA_TENANT_ACME_TOKEN=ghp_xxx

SCHEMA_TENANT_BETA_REPO=https://github.com/beta-corp/schemas.git
SCHEMA_TENANT_BETA_BRANCH=production
SCHEMA_TENANT_BETA_TOKEN=ghp_yyy
```

---

## Caching and refresh

| Behaviour | Detail |
|---|---|
| Clone on first use | Cloned to `SCHEMA_CACHE_DIR` (default `/tmp/schema_registry`) |
| TTL refresh | Re-fetched after `SCHEMA_TTL_SECONDS` (default 300 s) |
| Webhook invalidation | A push to the schema repo triggers an immediate re-clone for that tenant |
| Worker disk fallback | Celery workers read from the shared `SCHEMA_CACHE_DIR` volume without needing their own warm-up |
| Rule resolution cache | Resolved rules are cached in Redis for 300 s under key `rule:{tenant_id}:{platform}:{datasource}` |

To share the clone cache between the API process and workers in Docker Compose or Kubernetes, mount `SCHEMA_CACHE_DIR` as a shared volume.

---

## Relationship to `config.cue`

`config.cue` is the authoritative source for which tenants, platforms, and datasources exist. `bundle.json` is a **worker-side cache** of the relevant rules, formatted as flat JSON so workers can resolve `rule_config` without deserialising the full CUE bundle on every task.

The two should be kept in sync. When you add or change a rule in `config.cue`, update `bundle.json` in the schema repo and push — the schema webhook will cause workers to pick up the new rules within seconds.
