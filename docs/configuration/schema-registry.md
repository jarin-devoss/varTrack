# Schema Registry

The schema registry is a Git repository that holds two things:

1. **CUE schema files** — used to validate config files before any write
2. **`bundle.json` / `rules.json`** — the tenant's layer of rule settings, laid over the rules in `config.cue` (see [How a rule is built](#how-a-rule-is-built))

---

## Repo structure

```
my-org/schemas/                  ← schema registry repo
├── bundle.json                  ← the tenant's rule settings (optional)
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

## How a rule is built

varTrack builds the rule a sync runs with in layers: the admin's config, the tenant's config and the repository's own. Each layer overrides the one before it, key by key:

| # | Layer | Where |
|---|---|---|
| 1 | The rule in `config.cue` | the bundle every service reads |
| 2 | The tenant's rule | `bundle.json` in the tenant's schema repo — optional |
| 3 | The repository's overrides | `vartrack.json` in the pushed repo — optional ([Per-Repo Overrides](per-repo-overrides.md)) |
| 4 | `force` | the `config.cue` rule's `force` block |

`branch_map`, `file_path_map`, `variables_map` and `prune` are merged key by key across layers: a `branch_map` of `{develop: staging}` in `bundle.json` adds to the `{main: production}` of `config.cue`. The `prune.protection` patterns of every layer add up — a layer can protect more keys, never fewer. Every other key is replaced.

`bundle.json` and `vartrack.json` can also pull in shared settings with `extends` — see [Presets](#presets-extends).

**Keys only `config.cue` sets.** These are dropped from `bundle.json` with a warning (`… sets mongo_uri, self_heal, which only config.cue sets — ignored`). The gateway routes pushes by `config.cue`'s repositories and the watcher heals the rules `config.cue` marks `self_heal`, so another value further down would split the services:

- the datasource's connection settings and credentials: every sink key a [datasource block](bundle.md#datasources) sets (`mongo_uri`, `database`, `collection`, `update_strategy`, `redis_hosts`, `s3_bucket`, `ssh_host`, …)
- the rule's `secret_manager`, `repositories`, `exclude_repositories`, `overrides`, `self_heal`, `force` and `tenants`

`force` cannot pin these either; they are set on the rule itself.

**Which rules exist.** The rules are `config.cue`'s, since the gateway only routes pushes to those. A `bundle.json` rule for a platform and datasource with no rule in `config.cue` is ignored with a warning. A `config.cue` rule that no tenant's `bundle.json` names still syncs, as tenant `default`'s. Its `repo_url` is derived from the platform's `endpoint` and the rule's only repository (`http://gitea:3000` + `acme/configs` → `http://gitea:3000/acme/configs.git`); a webhook for a rule covering several repositories reads the pushed one.

A bundle whose `config.cue` has no `rules` gets only the second layer: `bundle.json`'s rules are used as they are, and a datasource's `config.cue` settings still win over the same keys in a rule.

---

## Presets (`extends`)

A preset is a JSON file of rule settings in a Git repository, shared by every `bundle.json` or `vartrack.json` that names it in `extends`. A presets repository might hold:

```
my-org/presets/
├── default.json        ← {"prune": {"protection": ["_vt_*"]}}
├── production.json     ← {"extends": ["local>my-org/presets"], "prune": {"protection": ["manual.*"]}}
└── repos/
    └── checkout.json   ← {"prune": {"protection": ["features.*"]}}
```

A preset is named like this:

| `extends` entry | File |
|---|---|
| `local>my-org/presets` | `default.json` in `my-org/presets` |
| `local>my-org/presets:production` | `production.json` |
| `local>my-org/presets//repos/checkout` | `repos/checkout.json` |
| `local>my-org/presets:production#v2` | `production.json` on branch or tag `v2` (default: `main`) |
| `gitea>my-org/presets:production` | the same file on `config.cue`'s platform `gitea` (any platform name: `github`, `gitea-team`, …), read with that platform's `token` |

`local>` (or no prefix) is the Git server of the file naming the preset: the schema repo's for `bundle.json`, the pushed repo's for `vartrack.json`, and the preset's own inside a preset.

How presets are resolved:

- presets are merged left to right, then the settings beside `extends` go on top;
- a preset can extend presets itself; one already in the chain is skipped;
- a preset's `description` is dropped;
- a preset that cannot be read, or is not a JSON object, is an error: a tenant whose `bundle.json` names one has no rules until it is fixed (logged as `bundle.json cannot be used — the tenant has no rules until it is fixed: …`), and a sync whose `vartrack.json` names one fails.

The same holds for a `bundle.json` that does not parse: the tenant does not fall back to the `config.cue` rules without its layer.

A preset adds to its layer only: presets that a `bundle.json` extends are part of the tenant's layer, so keys only `config.cue` sets are dropped from them too, and presets that a `vartrack.json` extends are limited to its [overridable keys](per-repo-overrides.md#overridable-keys). A changed preset is picked up when the tenant's schema repo is next refreshed (every `SCHEMA_TTL_SECONDS`, or on a push to it); a `vartrack.json`'s presets are read on every sync.

---

## `bundle.json`

`bundle.json` is a JSON array of rule objects — one entry per platform + datasource the tenant syncs. Each entry holds only what the tenant adds to, or changes in, the `config.cue` rule for that platform and datasource.

### Format

Two formats are accepted:

**Array (recommended):**
```json
[
  { ... rule object ... },
  { ... rule object ... }
]
```

**Object with a `rules` key** — which can also `extends` presets for every rule in it:
```json
{
  "extends": ["local>my-org/presets:production"],
  "rules": [
    { ... rule object ... }
  ]
}
```

A rule can `extends` presets of its own as well; they come after the file's.

### Example

With this rule in `config.cue`:

```cue
rules: [{
  platform:     "github"
  datasource:   "mongo"
  repositories: ["acme/app-configs"]
  file_name:    "configs/app.yaml"
  branch_map:   {main: "production"}
  self_heal:    true
  force:        {prune: {enabled: false}}   // production never prunes
}]
```

the tenant's `bundle.json` adds a staging branch, a root key and the smart-repair sync mode:

```json
[
  {
    "platform":   "github",
    "datasource": "mongo",
    "tenant_id":  "acme",
    "branch_map": {"develop": "staging"},
    "root_key":   "vartrack",
    "sync_mode":  "GIT_SMART_REPAIR",
    "prune":      true
  }
]
```

The rule a sync runs with: `file_name` `configs/app.yaml`, `branch_map` `{main: production, develop: staging}`, `root_key` `vartrack`, `sync_mode` `GIT_SMART_REPAIR`, `self_heal` true, `prune` `{enabled: false}` (forced), and the `mongo` datasource's connection settings from `config.cue`.

### Rule object fields

| Field | Required | Description |
|---|---|---|
| `platform` | Yes | Platform name — `github`, `gitea`, or `{type}-{tag}`; with `datasource`, picks the `config.cue` rule |
| `datasource` | Yes | Datasource name — `mongo`, `redis-cfg`, `s3-minio`, … |
| `tenant_id` | No | Tenant this rule belongs to (default: the schema repo's tenant) |
| `repo_url` | No | HTTPS clone URL of the config repository (default: derived from the platform and the rule's only repository) |
| `file_name` | No | Single file path to track in all repos |
| `file_path_map` | No | `{ env → file_path }` for per-environment file selection — merged |
| `branch` | No | Branch to heal when there is no `branch_map` (default: `main`) |
| `max_changed_files` | No | The most files one push syncs (default: 500). A push that changed more is cut to the first of them, with a warning in the log |
| `branch_map` | No | `{ branch → env }` label mapping — merged |
| `variables_map` | No | Merged |
| `sync_mode` | No | `AUTO`, `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, `LIVE_STATE` (`FULL` reads as `GIT_UPSERT_ALL`) |
| `prune` | No | `true`, or `{enabled, last, dry_run, protection}` — merged with the other layers; `protection` patterns add up, and an object with only `protection` does not enable pruning |
| `prune_protection` | No | Former flat name of `prune.protection`, still read. `prune_last` and `prune.last` were removed and are ignored |
| `root_key` | No | Subtree key to extract before flattening (default: `"vartrack"`) |
| `branch_as_env` / `pr_as_env` / `tag_as_env` | No | Environment from the branch name, `pr-{number}`, or the tag |
| `destination_template`, `dry_run`, `strict_validation` | No | As in [`config.cue`](bundle.md#rules) |
| sink options with no `config.cue` field | No | e.g. `key_format`, `helm_values_mode` |
| `extends` | No | [Presets](#presets-extends) for this rule, after the file's |

`repositories`, `self_heal`, `secret_manager` and the sink connection settings are set in `config.cue` only (see above).

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
| Rule resolution cache | The tenant's `bundle.json` rule is cached in Redis for 300 s under key `rule:{tenant_id}:{platform}:{datasource}`; the other layers are laid over it on every lookup, so a `config.cue` change applies at once |

To share the clone cache between the API process and workers in Docker Compose or Kubernetes, mount `SCHEMA_CACHE_DIR` as a shared volume.

---

## Relationship to `config.cue`

`config.cue` decides which platforms, datasources and rules exist, how to connect to each datasource, and which settings no tenant may change (`force`). `bundle.json` is the tenant's layer on top: nothing in it repeats `config.cue`, and nothing has to be kept in sync. A push to the schema repo makes workers pick up a changed `bundle.json` within seconds.
