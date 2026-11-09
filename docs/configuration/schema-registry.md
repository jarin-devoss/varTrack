# Schema Registry

The schema registry is a Git repository that holds two things:

1. **CUE schema files** — used to validate config files before any write
2. **`bundle.json` / `rules.json`** — the tenant's layer of rule settings, laid over the rules in `config.cue` (see [How a rule is built](#how-a-rule-is-built))

---

## Repo structure

```
my-org/schemas/                  ← schema registry repo
├── bundle.json                  ← the tenant's rule settings (optional)
├── app.yaml.cue                 ← CUE schema for every file named app.yaml
├── config.toml.cue              ← CUE schema for every file named config.toml
├── .env.cue                     ← CUE schema for every file named .env
├── default.cue                  ← schema for any other file (optional)
├── teams/
│   └── payments.cue             ← a schema a `schema_map` entry names (optional)
└── presets/
    └── team.json                ← a preset bundle.json can extend (optional)
```

Schemas found by a file's name are read from the repository's root only. A schema in a folder is used when a rule's [`schema_map`](#which-schema-a-file-gets) names it. See [CUE schemas](#cue-schemas) for what goes in one.

The schema repo is pointed to from `config.cue`:

```cue
schema_registry: {
  platform: "github"
  repo:     "my-org/schemas"
  branch:   "main"
}
```

varTrack clones this repo for each tenant on first use and refreshes it automatically on a TTL (default 5 minutes) or immediately when the schema repo itself receives a push webhook.

**A tenant is something the operator declares; a request only names one.** The repository, branch and credential of a tenant's schema repo are its `schema_repo` in [`tenants`](bundle.md#tenants) (the `SCHEMA_TENANT_<ID>_*` variables still work for one release and log a warning), and nothing a request carries can add or change them:

- A **schema push** belongs to the tenant whose registered schema repository it came from. A push from a repository no tenant registered is refused (`404` on the HTTP route, `NOT_FOUND` over gRPC), and what is re-read is the registered repository with the registered credential, never the pushed one and never a token from the webhook's headers. A push to a branch the registry does not follow is ignored. A `tenant_id` in the body, when there is one, must be that tenant's.
- `POST /v1/tenants/{tenant}/schemas/warm` takes no repository, branch or token; an unregistered tenant is `404`.
- A tenant name is letters, digits, `-` and `_`, starting with a letter or digit, up to 63 characters, wherever it is given (a header, a body, a URL, `vt --tenant`); anything else is `400`.
- **A file never says which tenant it belongs to.** A rule's tenant is the tenant whose schema repository holds the `bundle.json` that layers it, or, for a `config.cue` rule no `bundle.json` names, the first of its `tenants` (else the declared tenant whose repositories it covers, else `default`). A `tenant_id` written in a `bundle.json` or a `vartrack.json` is not a setting: a sync drops it with a warning, and `vt validate-config` reports it as an error. A file that could name its own tenant could act as another one.

---

## CUE schemas

A schema says which keys a config file must have and what their values may be. varTrack checks every file a sync reads against its schema before writing it to a sink.

### Which schema a file gets

In this order, the first that applies:

| # | Source | When it names a schema the schema repository does not have |
|---|---|---|
| 1 | the first [`schema_map`](#schema_map-a-schema-for-a-path) entry whose `match` matches the file's path | **the file fails**, `strict_validation` or not |
| 2 | the file's own `$schema` key, when the rule sets [`schema_from_file`](#a-file-names-its-own-schema) | **the file fails**, the same way |
| 3 | `<file name>.cue` at the schema repo's root, by the file's name without its folder: `configs/app.yaml` and `services/api/app.yaml` both get `app.yaml.cue` | not a miss: go on |
| 4 | `default.cue` | not a miss: go on |
| 5 | none: the file is **not checked** | with [`strict_validation`](rule-options.md#strict_validation) the file fails |

An explicit choice that cannot be honoured is an error, never a quiet fall-back to the next step: the failure reads `schema api/settings.cue (schema_map[0], services/api/**/*.yaml) is not in the schema repository of tenant acme at 5c4e1f0a2b3c`. The file is not written, so a placeholder its missing schema would have replaced with a [`@secret()`](annotations.md#secret) value never reaches the store.

If the `cue` binary is not on the orchestrator's `PATH` (or at `CUE_BINARY`), files are not checked and a warning is logged; with `strict_validation` they fail.

**Every file says what checked it.** A task's files, a dry run's report, the web UI and `vt validate` show `validation` (`ok`: checked and passed, `warn`: checked, failed, not strict, `failed`: strict and failed, `none`: nothing checked it), the `schema` (its path in the schema repository, or `none`) and the `schema_source` (`schema_map[N]`, `file`, `name`, `default` or `none`). Next to the task's `schema_commit`, that answers "which schema, at which commit, checked this file?". A file nothing checked reports `none`, never `ok`.

#### `schema_map`: a schema for a path

```cue
rules: [{
  platform: "gitea", datasource: "mongo", repositories: ["acme/configs"]
  schema_map: [
    {match: "services/api/**/*.yaml",  schema: "api/settings.cue"},
    {match: "services/web/config.yaml", schema: "web.cue"},
    {match: "legacy/**",                schema: "none"},   // on purpose not checked
  ]
}]
```

- `match` is a glob over the file's path in its repository, with forward slashes: `*` stays inside one folder, `**` crosses folders (`services/**/app.yaml` also matches `services/app.yaml`), `?` is one character, `[abc]` a set. Dotfiles match (`.env`). Case counts. There are no regular expressions.
- `schema` is a path inside the schema repository ending in `.cue`, or `none` for "not checked, on purpose". An absolute path, a `..` segment or a backslash is refused when the configuration loads; at sync time the path is joined to the clone and has to stay inside it, symlinks included.
- **The first match wins.** A file no entry matches is found by its name, as before: `schema_map` adds to the name lookup, it does not replace it.
- `config.cue` and a tenant's `bundle.json` may set it; a repository's `vartrack.json` may not, because the people who write the data must not choose what checks it. The layers **add up**, `config.cue`'s entries first, so a path the operator mapped cannot be remapped by a tenant.
- Every entry is checked when the configuration loads, and all the bad ones are named in one error. For a `bundle.json` that makes the tenant's `bundle.json` unusable until it is fixed, as for any invalid `bundle.json`. When the schema repository is cloned or refreshed, an entry whose schema is not in it is logged once with the rule that names it.

#### A file names its own schema

With `schema_from_file: true` in the rule (`config.cue` or `bundle.json`), a top-level `$schema` key in a JSON, YAML or TOML file names its schema, in the syntax of a `schema_map` `schema`:

```yaml
$schema: teams/payments.cue
database:
  host: db.internal
```

- It is read only when **no `schema_map` entry matched** the file. If one did and the file names a different schema, the map's schema is used and the file's result says so (`the file's $schema (…) was overridden by schema_map[0]`): the operator outranks the one who writes the data.
- A URL (`https://json.schemastore.org/…`, an editor's hint) is not a schema in the schema repository: it is ignored, with a note in the file's result.
- **`$schema` is never synced as a key**, whether it is honoured or not. Editors add it to YAML and JSON files for completion; it is taken out before the file is flattened.

### What a schema sees

A schema checks the data as the sink receives it, not the file as written: after [`root_key`](rule-options.md#root_key) is taken out, the [environment slice](../concepts/environment-resolution.md) is picked, the data is flattened to dotted keys, [`variables_map`](rule-options.md#variables_map) renames are applied and [`@secret()`](annotations.md#secret) values are read.

**Every value is a string by then:** `5432` is `"5432"`, `true` is `"true"`, `null` is `""`. Write `string & =~"^[0-9]+$"` for a number — `int` rejects every value.

### Example

The config repository has `configs/app.yaml`:

```yaml
app:
  name: checkout
database:
  host: db.internal
  port: 5432
log:
  level: info
features:
  dark_mode: true
```

which the schema sees as:

```json
{
  "app.name":           "checkout",
  "database.host":      "db.internal",
  "database.port":      "5432",
  "log.level":          "info",
  "features.dark_mode": "true"
}
```

The schema repo has `app.yaml.cue`:

```cue notest
// Required: a file without one of these keys fails.
"app.name":      string & !=""
"database.host": string & !=""
"database.port": string & =~"^[0-9]{1,5}$"

// One of a fixed set of values.
"log.level": "debug" | "info" | "warn" | "error"

// Optional: checked only when the file has it.
"features.dark_mode"?: "true" | "false"
```

This file passes. With `port: 55x` and `level: verbose` it fails with:

```
"log.level": 4 errors in empty disjunction: …
"database.port": invalid value "55x" (out of bound =~"^[0-9]{1,5}$")
```

and without `app.name` with `"app.name": incomplete value !=""`.

Keys the schema does not name are allowed. To reject them, wrap the schema in `close({ … })`:

```cue notest
close({
  "app.name":      string & !=""
  "database.host": string & !=""
})
```

A field can also carry [`@secret()` and `@logger()`](annotations.md).

### When a file fails

| [`strict_validation`](rule-options.md#strict_validation) | The file |
|---|---|
| `false` (default) | is written; the failure is logged as a warning and the file's validation is `warn` |
| `true` | is not written; its validation is `failed`. The push's other files still sync |

With `strict_validation` **every file has to be checked**: a file no schema checks (step 5 above) fails with `strict_validation: no schema checks configs/app.yaml (rule gitea/mongo): add a schema_map entry for it, or map it to "none" to leave it unchecked on purpose`. A rule that relied on "strict, but nothing checks this file" needs one `schema_map` entry for it.

[`vt validate`](../cli/index.md#vt-validate) always treats a failure as an error and exits 1, so a pull request check catches it before the merge.

To try a schema on your machine, put the flat form above in `data.json` and run `cue vet app.yaml.cue data.json`.

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

**Keys only `config.cue` sets.** These are dropped from `bundle.json` with a warning (`… sets endpoint, self_heal, which only config.cue sets — ignored`). The gateway routes pushes by `config.cue`'s repositories and the watcher heals the rules `config.cue` marks `self_heal`, so another value further down would split the services:

- the datasource's connection settings and credentials: every field of a [datasource block](bundle.md#datasources) (`endpoint`, `database`, `collection`, `update_strategy`, `hosts`, `bucket`, `host`, …). A sink reads them from that block alone, under the names the block uses; a rule's copy of one, in any layer, is never read. The old flat names (`mongo_uri`, `s3_bucket`, `ssh_host`, …) are not options at all and are dropped as undeclared.
- these rule options:

<!-- rule-options:admin-only -->
- [`exclude_repositories`](rule-options.md#exclude_repositories)
- [`force`](rule-options.md#force)
- [`overrides`](rule-options.md#overrides)
- [`repositories`](rule-options.md#repositories)
- [`secret_manager`](rule-options.md#secret_manager)
- [`self_heal`](rule-options.md#self_heal)
- [`tenants`](rule-options.md#tenants)
<!-- /rule-options:admin-only -->

`force` cannot pin these either; they are set on the rule itself.

**Which rules exist.** The rules are `config.cue`'s, since the gateway only routes pushes to those. A `bundle.json` rule for a platform and datasource with no rule in `config.cue` is ignored with a warning. A `config.cue` rule that no tenant's `bundle.json` names still syncs, as tenant `default`'s. Its `repo_url` is derived from the platform's `endpoint` and the rule's only repository (`http://gitea:3000` + `acme/configs` → `http://gitea:3000/acme/configs.git`); a webhook for a rule covering several repositories reads the pushed one.

A bundle whose `config.cue` has no `rules` gets only the second layer: `bundle.json`'s rules are used as they are. Where the sink connects still comes from the datasource's block in `config.cue` only; a datasource `config.cue` does not declare runs on the sink's defaults.

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

**Where a tenant's presets may come from.** Once [`tenants`](bundle.md#tenants) are declared, a preset is read only from a repository the tenant owns: one of its `repositories` (with the platform credential a sync of them uses), its `schema_repo` (with that repository's `token`), or one its `presets` lists (with its `presets_token`). A preset anywhere else is refused with the preset named, before anything is fetched, and the platform's own token is never sent to it; `vt validate-config` reports it. Without declared tenants presets are unrestricted, as before.

So an entry reads as **`<server>><owner>/<repo>`**, then optionally **`:<file>`** (a file at the repository's root, without `.json`) or **`//<path>`** (a file in a folder, without `.json`), then optionally **`#<branch or tag>`**. With neither `:` nor `//` the file is `default.json`.

**Example — one setting shared by every checkout repository.** Create a repository `my-org/presets` with the file `repos/checkout.json`:

```json
{
  "description": "Settings every checkout service repository uses",
  "prune": {"protection": ["features.*"]},
  "branch_map": {"release": "production"}
}
```

Then each repository that wants it names it in its `vartrack.json`, and adds what is its own:

```json
{
  "extends": ["local>my-org/presets//repos/checkout"],
  "root_key": "checkout"
}
```

The sync of that repository runs with `features.*` added to the rule's `prune.protection`, `release: production` added to its `branch_map`, and `root_key` `checkout`. Change `repos/checkout.json` once and every repository that extends it gets the change on its next sync. A preset can hold any option the file extending it may set — for a `vartrack.json` that is the [overridable keys](per-repo-overrides.md#overridable-keys); `description` is only a note and is dropped.

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
    "branch_map": {"develop": "staging"},
    "root_key":   "vartrack",
    "sync_mode":  "GIT_SMART_REPAIR",
    "prune":      true
  }
]
```

The rule a sync runs with: `file_name` `configs/app.yaml`, `branch_map` `{main: production, develop: staging}`, `root_key` `vartrack`, `sync_mode` `GIT_SMART_REPAIR`, `self_heal` true, `prune` `{enabled: false}` (forced), and the `mongo` datasource's connection settings from `config.cue`.

### Rule object fields

Every option, with the files that may set it, is in [Rule options](rule-options.md). The fields of a `bundle.json` rule:

| Field | Required | Description |
|---|---|---|
| `platform` | Yes | Platform name — `github`, `gitea`, or `{type}-{tag}`; with `datasource`, picks the `config.cue` rule |
| `datasource` | Yes | Datasource name — `mongo`, `redis-cfg`, `s3-minio`, … |
| `repo_url` | No | HTTPS clone URL of the config repository (default: derived from the platform and the rule's only repository) |
| `file_name` | One of the two | Single file path to track in all repos. A rule that names neither this nor `file_path_map` is refused |
| `file_path_map` | One of the two | `{ env → file_path }` for per-environment file selection — merged |
| `branch` | No | The rule's own branch: it feeds the `file_path_map` environments, and self-heal compares it when there is no `branch_map` (default: `main`) |
| `branch_map` | No | `{ branch → env }` label mapping — merged |
| `variables_map` | No | Key renames, `{NEW_NAME: flat.key}` — merged |
| `sync_mode` | No | `AUTO`, `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, `LIVE_STATE` (`FULL` reads as `GIT_UPSERT_ALL`) |
| `prune` | No | `true`, or `{enabled, protection}` — merged with the other layers; `protection` patterns add up, and an object with only `protection` does not enable pruning |
| `prune_protection` | No | Former flat name of `prune.protection`, still read. `prune_last` and `prune.last` were removed and are ignored |
| `root_key` | No | Subtree key to extract before flattening (default: `"vartrack"`) |
| `branch_as_env` / `pr_as_env` / `tag_as_env` | No | Environment from the branch name, `pr-{number}`, or the tag |
| `destination_template`, `strict_validation` | No | As in [`config.cue`](bundle.md#rules) |
| `schema_map`, `schema_from_file` | No | Which schema checks which file; see [Which schema a file gets](#which-schema-a-file-gets). `schema_map` adds to `config.cue`'s entries |
| `key_format`, `file_format`, `env_as_filename`, `helm_values_mode`, `s3_object_tags`, `s3_tag_keys` | No | Sink layout options with no `config.cue` datasource field: how a key is flattened, how a file or object is laid out. They never say where a destination is or how to reach it, so a tenant may set them. See [Rule options](rule-options.md) |
| `extends` | No | [Presets](#presets-extends) for this rule, after the file's |

`repositories`, `self_heal`, `secret_manager` and the sink connection settings are set in `config.cue` only (see above). **A key that is not an option at all is dropped too**, with a warning that names it (`… sets helm_binary, which is not an option a tenant may set — ignored`): a `bundle.json` can set exactly the options listed here, whatever a sink happens to read. Helm's `extra_flags` and `values_file_path` are fields of the `helm` datasource in `config.cue`; which `helm` program runs is the orchestrator's `HELM_BINARY` environment variable.

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

Tenants are declared in `config.cue`'s [`tenants`](bundle.md#tenants). Before that existed they were registered with environment variables on the orchestrator-service and its Celery workers; those still work for one release, log a warning, and lose to a declared tenant of the same name:

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
