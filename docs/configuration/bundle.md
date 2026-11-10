# Bundle Reference

All three varTrack services read a single shared CUE bundle (`config.cue`), at the path in `CONFIG_PATH`. The bundle declares your platforms, datasources, routing rules, schema registry, and secret managers. It is the first of the files a rule is built from — see [Configuration files](index.md) for where the others live and how they combine.

---

## Minimal example

```cue
bundle: {
  platforms: [{
    github: {
      endpoint:        "https://github.com"
      push_event_name: "push"
      pr_event_name:   "pull_request"
      secret:          "my-webhook-secret"
    }
  }]

  datasources: [{
    mongo: {
      endpoint:   "mongodb://localhost:27017"
      database:   "vartrack"
      collection: "variables"
    }
  }]

  rules: [{
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/app.yaml"
    repositories:         ["my-org/*"]
    destination_template: "{env}-config"
    self_heal:            true
    branch_map: {
      main:    "production"
      develop: "staging"
    }
  }]
}
```

---

## What the loader checks

Every service reads `config.cue` through one loader and refuses a config it cannot accept; `vartrack-bundle check config.cue` says why, and CI runs it. In order:

1. **Names.** A field the protos do not have, a typo, is refused, and so is a value an enum does not have (`sync_mode: "FAST"`), with the values it does.
2. **Shorthands.** `"30s"`, `prune: true` and a secret written as a plain string are read as what they stand for.
3. **The rules the protos declare.** Ranges (`redis.database` is 0 to 15), lengths, allowed values, and the rules that tie fields together: a Mongo `username` needs its `password`; a Mongo needs a `collection` unless `env_as_collection` is on; `host_key_verification: "accept-new"` needs `known_hosts_file`; a ZooKeeper datasource is `STRATEGY_KEY_VALUE` or unset.

A rule on a field applies when the config **sets** the field, or when the field is required: at least one platform, datasource and rule, a rule's `repositories`, a datasource's `hosts`. A field you leave out is its default, and is not held to a rule. Every broken rule is listed at once, each with the path of the field:

```text
the config breaks 2 rules its protos declare:
  - datasources[1].redis.database: value must be greater than or equal to 0 and less than or equal to 15
  - datasources[2].mongo: Password is required when username is provided
```

The rules about a rule as a whole (`file_name` or `file_path_map`, `{env}` in a `destination_template` needing an environment strategy) are about the *merged* rule, `config.cue`'s layer with the tenant's `bundle.json` and a repository's `vartrack.json` laid over it, so they are not asked of `config.cue` on its own.

---

## `platforms`

Declares which Git platforms send webhooks to varTrack.

### GitHub

```cue
platforms: [{
  github: {
    endpoint:        "https://github.com"     // or your GHE URL
    protocol:        "https"                  // "https" or "ssh"
    push_event_name: "push"
    pr_event_name:   "pull_request"
    secret:          "my-secret"              // webhook HMAC secret
    token:           "ghp_xxx"               // PAT for cloning + repo listing
    org_name:        "my-org"                 // optional — scope to one org
    timeout:         "30s"
  }
}]
```

Multiple platform instances can be declared with different `tag` values:

```cue
platforms: [
  { github: { tag: "public",     endpoint: "https://github.com", ... } },
  { github: { tag: "enterprise", endpoint: "https://git.mycompany.com", ... } },
]
```

---

## `datasources`

Declares where config values should be written. Every datasource has an optional `tag` to distinguish multiple instances of the same type.

### MongoDB

```cue
datasources: [{
  mongo: {
    tag:             ""                        // optional — "mongo-{tag}" if set
    endpoint:        "mongodb://mongo:27017"
    database:        "vartrack"
    collection:      "variables"
    update_strategy: "STRATEGY_DOCUMENT"       // the default; or "STRATEGY_KEY_VALUE", "STRATEGY_FILE": see the MongoDB sink
  }
}]
```

### Redis

```cue
datasources: [{
  redis: {
    hosts:    ["redis:6379"]
    password: "secret"
    database: 0
  }
}]
```

### ZooKeeper

```cue
datasources: [{
  zookeeper: {
    hosts:    ["zookeeper:2181"]
    base_path: "/vartrack"
  }
}]
```

### S3

```cue
datasources: [{
  s3: {
    bucket:           "my-config-bucket"
    region:           "us-east-1"
    access_key_id:     "AKIA..."
    secret_access_key: "..."
  }
}]
```

For all datasource options see the individual [sink pages](../sinks/index.md).

---

## `rules`

Rules map a platform + file combination to a datasource and define how data is written. They are the first of the [layers a rule is built from](schema-registry.md#how-a-rule-is-built): a tenant's `bundle.json` and a repository's `vartrack.json` can change some of their settings, and `force` pins the ones they may not.

A rule is identified by its `platform` and `datasource`. Each pair has at most one rule, and varTrack refuses to start when two share it; to write a second destination in the same database, declare a tagged datasource for it and give it its own rule. Rules on **different** platforms may write to the same datasource: a webhook is then told apart by the event header of the platform that sent it (`X-Gitea-Event`, `X-GitHub-Event`), and `vt sync` and `vt validate` need `--platform` to say which rule they mean.

Two platforms of the **same type** (say two GitHub hosts) send the same headers, so the webhook secret tells them apart: the gateway checks the signature of the delivery against each candidate platform's `secret` and uses the one that signed it. Give each of them its own `secret`. A delivery signed by none of them is refused (`401`); when two share a secret, or none has one, or signatures are not checked (a non-production gateway), the webhook is refused as ambiguous (`400`).

```cue
rules: [{
  platform:             "github"              // matches platform name (or "github-{tag}")
  datasource:           "mongo"               // matches datasource name (or "mongo-{tag}")
  repositories:         ["my-org/*"]          // glob patterns — which repos trigger this

  // Where data lands
  destination_template: "{env}-config"

  // Source (pick one)
  file_name:     "configs/app.yaml"           // single file tracked in all repos
  // file_path_map: { production: "configs/prod.yaml", staging: "configs/staging.yaml" }

  // Repository filtering
  exclude_repositories: ["my-org/legacy-.*"] // negative glob patterns
  // overrides: [{match_repositories: ["other-org/*"], enable: true}]

  // Root key extraction (optional)
  root_key: "vartrack"                        // extract subtree before flattening
                                              // set to "" to process the whole file

  // Key renames (optional): NEW_NAME ← the flat key in the file
  variables_map: {
    DB_HOST: "database.host"                  // writes database.host's value as DB_HOST
  }

  // Environment resolution
  branch_map: {
    main:    "production"
    develop: "staging"
  }
  pr_as_env:     false
  branch_as_env: false
  tag_as_env:    false

  // Sync behavior
  sync_mode:     "AUTO"                       // AUTO, GIT_UPSERT_ALL, GIT_SMART_REPAIR
                                              // (SYNC_MODE_AUTO etc. work too)
  prune: {
    enabled:    false                         // delete keys no longer in Git
    protection: ["SYSTEM_*", "_vt_*"]         // glob patterns for keys that must never be deleted
  }

  // Validation
  strict_validation: false                    // true: a file the CUE schema rejects, or no schema checks, fails the sync
                                              // (default: the failure is logged and the file is synced)
  schema_map: [                               // which schema checks which path; the first match wins
    {match: "services/api/**/*.yaml", schema: "api/settings.cue"},
    {match: "legacy/**",              schema: "none"},        // on purpose not checked
  ]
  schema_from_file: false                     // true: a file's top-level $schema may name its schema (never over schema_map)

  // Drift detection
  self_heal:     true                         // auto-repair on drift
  drift_ignore:  ["feature_flags.*"]          // keys the application changes itself: shown, never drift

  // Pausing
  suspend:       false                        // true: pushes and self-heal skip the rule

  // Keys no tenant bundle.json or repo vartrack.json can change: applied last
  force: {prune: {enabled: false}}
}]
```

`repositories`, `exclude_repositories`, `overrides`, `self_heal` and `secret_manager` are set here only, as are the datasources' connection settings: a tenant's `bundle.json` cannot change them. `force` holds the other keys this rule pins.

**`suspend`** pauses the rule without removing it, for an incident or a migration. A push and the watcher's self-heal skip a suspended rule, and the drift check does not compare it. A sync started by hand, from the web UI or `vt`, still runs, so an incident or a migration does not block a deliberate sync. The web UI shows the rule as suspended.

**`drift_ignore`** lists glob patterns of keys the application changes itself, such as a feature-flag counter or a rotated token. The drift check still shows them, as `ignored`, but never counts them as drift and the watcher does not heal them; a sync still writes them.

**`strict_validation`** decides what a failed CUE schema check does. Off (the default), the failure is logged as a warning and the file is synced; on, the file is not written and the sync fails. On also means every file is checked: a file no schema covers fails too, unless a `schema_map` entry maps it to `none`. A repository can turn it on in its `vartrack.json`.

**`schema_map`** sends file paths to CUE schemas, and **`schema_from_file`** lets a file name its own; [Which schema a file gets](schema-registry.md#which-schema-a-file-gets) has the lookup order and what each miss means.

---

## Per-repo overrides

A repository can change some of its rule's settings with a `vartrack.json`, and a tenant with its `bundle.json`; the rule's `force` keys are applied after both. [Configuration files](index.md#how-a-rule-is-built) shows how the layers combine, [Rule options](rule-options.md) which option each file may set, and [Per-Repo Overrides](per-repo-overrides.md) where `vartrack.json` lives.

---

## `schema_registry`

Optional: a tenant declares its own schema repository in [`tenants`](#tenants), and this block is the one every rule shares. It points varTrack to a Git repo containing CUE schemas for validation:

```cue
schema_registry: {
  platform: "github"
  repo:     "my-org/schemas"
  branch:   "main"
}
```

varTrack checks every file a sync reads against the tenant's CUE schema for it before writing anything. A file that fails is skipped when the rule sets `strict_validation`, and written with a warning otherwise — see [CUE schemas](schema-registry.md#cue-schemas) for how a file's schema is picked and an example.

The schema repo can also hold `bundle.json` (or `rules.json`): the tenant's rule settings, laid over the rules here. It only holds what the tenant adds or changes — nothing has to be repeated or kept in sync. See the [Schema Registry reference](schema-registry.md) for how a rule is built from the layers, the full repo structure, the `bundle.json` format, tenant env vars, and caching behaviour.

---

## `tenants`

A tenant is a team you declare here. Without a `tenants` block there is one implicit tenant, `default`, that covers every repository, and nothing below applies.

```cue
tenants: [
  {
    name:         "payments"
    repositories: ["payments/*"]        // pushes from these repositories are payments'
    schema_repo: {                      // its bundle.json, CUE schemas and presets
      platform: "github"
      repo:     "payments/schemas"
      branch:   "main"
      token:    {ref: {path: "payments/git", key: "token"}}
    }
    destinations: [{datasource: "redis", paths: ["payments:*"]}]
  },
  {name: "search", repositories: ["search/app", "search/infra"]},
]
```

| Field | Meaning |
|---|---|
| `name` | Letters, digits, `-` and `_`, starting with a letter or digit, up to 63 characters. |
| `repositories` | `owner/name` patterns. A push is the tenant of the repository it came from; no repository may belong to two tenants. |
| `schema_repo` | The tenant's schema repository (`platform`, `repo`, `branch`, `token`). A push to it refreshes this tenant and no other, and it is read with this tenant's own `token`. |
| `destinations` | What the tenant may write, per datasource: `datasource` and glob `paths`. Two tenants may not both be allowed one destination of a datasource. |
| `presets` / `presets_token` | Repositories, besides the tenant's own and its schema repository, its presets may be read from, and the credential for them. |

A rule's `tenants` lists the tenants whose `bundle.json` may layer it; once tenants are declared, each name in it must be one of them. A rule that lists none belongs to the declared tenant whose `repositories` it covers (else `default`). A push that no declared tenant owns is refused (`403` on the HTTP route, `PERMISSION_DENIED` over gRPC), and a tenant named in a header or a grant is checked against the repository the push came from, never used in its place.

The loader refuses a `tenants` block with a bad name, a repeated name, a repository or a schema repository that belongs to two tenants, overlapping destinations on one datasource, or a rule that names a tenant nobody declared.

---

## TLS to your Git platforms, Vault and S3

The server's certificate is **always checked**, by every service, unless the setting says otherwise. The
setting is named for the unsafe choice, so leaving it out is the safe one:

| Setting | On | Meaning |
|---|---|---|
| `insecure_skip_verify: true` | `platforms[]` (`github`, `gitea`), `secret_managers[].vault`, `datasources[].s3` | Do not check the server's certificate. Leave it out to check |
| `ssl_ca` | `vault` | The CA certificate **as content** (PEM) |
| `ssl_ca_file` | `vault` | The CA certificate **as a path** to a PEM file the service reads. Give this or `ssl_ca`, never both |
| `ssl_ca_cert` | `s3` | A CA bundle, **as a path** |

A field is either a path or content, never both: `ssl_ca` holding a file name is refused (older configs that did
that are read as `ssl_ca_file`, with a log line). `verify_ssl: false` is the former spelling of
`insecure_skip_verify: true`, and still loads; `verify_ssl: true` was the default and says nothing now. A Vault,
Gitea or GitHub behind a self-signed certificate therefore needs its CA, or `insecure_skip_verify: true`.

## `secret_managers`

Secrets referenced in the bundle (tokens, passwords) can be resolved from HashiCorp Vault:

```cue
secret_managers: [{
  vault: {
    endpoint:    "https://vault.mycompany.com"
    mount_point: "secret"
    kv_version:  2
    path_prefix: "myapp/prod"   // optional: where a plain @secret() field is read (see below)

    // Auth — pick one:
    token_auth: {
      token: "hvs.xxxx"
    }
    // app_role_auth: {
    //   role_id:   "role-id"
    //   secret_id: "secret-id"
    // }
    // kubernetes_auth: {
    //   role:            "vartrack"
    //   jwt_path:        "/var/run/secrets/kubernetes.io/serviceaccount/token"
    // }
    // userpass_auth: {
    //   username: "vartrack"
    //   password: "pass"
    // }
  }
}]
```

`path_prefix` is for the CUE schema's [`@secret()` fields](annotations.md#secret). A field whose value in the config file is written as `@secret(mount/path#key)` names its own secret and does not use it. A field left as a plain value is read from the secret at `path_prefix`, under the field's own name, so `database.password` is the key `database.password` of the secret `secret/myapp/prod`.

A rule used to carry its own Vault settings in a `secrets` block. That block was removed: declare the manager here, and name it in the rule's `secret_manager` when there is more than one. A config that still sets `secrets` loads, with a warning that says so.

---

## Secret value forms

Credential fields in the bundle — a platform's `token` and `secret`, a datasource's `password`, `private_key`, `access_key_id`, `secret_access_key`, the web UI's `client_id` and `client_secret` — accept three forms:

```cue notest
// 1. Plain string — value written directly in the bundle
token: "ghp_xxx"

// 2. File reference — read from a file on the service's machine
//    Ideal for Docker secrets, Kubernetes secret mounts, or any on-disk secret
token:    {file: "/run/secrets/github-token"}                  // the whole file, whitespace stripped
password: {file: "/run/secrets/db.env", key: "DB_PASSWORD"}    // one key of the file

// 3. Vault reference — fetched from HashiCorp Vault at ETL time
//    Requires a secret_managers block in the bundle
token: {ref: {path: "vartrack/github", key: "token"}}
```

A Vault secret manager's own login credentials (`token_auth.token`, `app_role_auth.secret_id`, `userpass_auth.password`) take a string or a `{file: …}`, never a `{ref: …}`: Vault cannot log in with a secret it would first have to read from itself, and the loader refuses it.

```cue
secret_managers: [{vault: {endpoint: "https://vault:8200", token_auth: {token: {file: "/run/secrets/vault-token"}}}}]
```

A `{ref: …}` is read from an entry of `secret_managers`. A rule's `secret_manager` names the entry for everything that rule uses — its platform's credentials and its datasource's — and for its `@secret()` fields:

```cue
rules: [{
  platform:       "github"
  datasource:     "redis"
  secret_manager: "vault-prod"    // a secret_managers entry: "vault", or "vault-{tag}"
}]
```

A rule without one uses the entry tagged `default`, else the bundle's only one.

With `key`, a `{file: …}` is parsed by its extension — `.json`, `.yaml`/`.yml`, otherwise `KEY=value` lines (`.env`, `.properties`) — and the key's value is the credential; in JSON and YAML a dotted key (`db.password`) reaches into nested objects. Without `key` the whole file is the credential. An SSH `private_key` is the exception: it must be a key file of its own.

`{file: …}` is read on the machine of the service that uses the credential: the gateway for platform credentials, the orchestrator for datasource credentials. A credential both use — a platform `token` — needs its file on both.

### `{file:}` — mounted secret files

The `{file:}` form keeps secrets completely out of the bundle. Mount the file via Docker or Kubernetes and point to it:

```cue
secret_managers: [{
  vault: {
    endpoint:    "https://vault.mycompany.com"
    mount_point: "secret"
    kv_version:  2
    token_auth: {
      token: "hvs.xxxx"   // Vault's own login is a plain string; mount the bundle as a secret
    }
  }
}]

platforms: [{
  github: {
    endpoint: "https://github.com"
    token:    {file: "/run/secrets/github-pat"}
    secret:   {file: "/run/secrets/github-webhook-secret"}
  }
}]
```

**Docker Compose example:**

```yaml
services:
  gateway:
    image: vartrack/gateway
    secrets:
      - github-pat
      - github-webhook-secret

secrets:
  github-pat:
    file: ./secrets/github-pat.txt
  github-webhook-secret:
    file: ./secrets/github-webhook-secret.txt
```

**Kubernetes example:**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: vartrack-secrets
stringData:
  github-pat: ghp_xxx
  github-webhook-secret: my-secret
---
# In the Pod spec:
volumeMounts:
  - name: vartrack-secrets
    mountPath: /run/secrets
    readOnly: true
volumes:
  - name: vartrack-secrets
    secret:
      secretName: vartrack-secrets
```

If the file cannot be read at startup, the service exits with a descriptive error.

---

## `global_tags`

Arbitrary key-value metadata applied to every write across all sinks:

```cue
global_tags: {
  team:        "platform"
  environment: "production"
}
```

---

## Infrastructure wiring

Wire varTrack's internal components to specific datasources. Each value is the datasource name (`{type}` or `{type}-{tag}`):

```cue
bundle: {
  celery_broker_datasource:            "redis"        // Celery task queue broker
  celery_backend_datasource:           "redis"        // Celery result backend
  gateway_nonce_datasource:            "redis"        // replay-protection nonce store
  watcher_leader_election_datasource:  "redis"        // distributed leader election (Redis or ZooKeeper)
}
```

In a single-Redis setup, all four can point to the same datasource. In production you may want separate instances for isolation. The `watcher_leader_election_datasource` also accepts `"zookeeper"`.

See [Drift Detection](../concepts/drift-detection.md) for how the watcher uses these.

---

## `ui`

The web UI: where users open it, and sign-in with your Git platforms. Each user sees the rules whose repositories their own account on the rule's platform can read, and syncs the ones it can write to. Without `ui.auth` the UI is open.

```cue
ui: {
  public_url: "https://vartrack.example.com"
  auth: {
    providers: [
      {platform: "github", client_id: "Iv1.8a61…", client_secret: {file: "/run/secrets/github-oauth"}},
      {platform: "gitea",  client_id: {file: "/run/secrets/gitea-id"}, client_secret: {ref: {path: "ui/gitea", key: "client_secret"}},
       browser_url: "https://gitea.example.com"},
    ]
    admins: ["github:octo-admin"]
    secret_manager: "vault"   // where {ref: …} values come from; optional with one secret manager
  }
}
```

Every field, and how sessions work: [Web UI sign-in](auth.md#web-ui-sign-in-with-your-git-platform). Full example: [`15_web_ui_sign_in.cue`](https://github.com/jarin-devoss/varTrack/blob/main/examples/bundles/15_web_ui_sign_in.cue).

---

## `auth`

The CLI API (`/v1/cli/*`, used by `vt` and the web UI's Playground) lets a caller do what their own Git token may do in the rule's repositories. `auth` adds an optional OPA policy on top:

```cue
auth: {
  opa: {policy_file: "/etc/vartrack/vartrack_authz.rego"}
}
```

How callers are checked, the OPA input document and the evaluation order: [Authentication & Authorization](auth.md). Full example: [`13_cli_auth_opa.cue`](https://github.com/jarin-devoss/varTrack/blob/main/examples/bundles/13_cli_auth_opa.cue).

---

## Gateway tuning

Fine-tune gateway-service behaviour directly from the bundle:

### `max_webhook_body_bytes`

Maximum size of an incoming webhook request body. Requests larger than this limit are rejected with **HTTP 413**. Defaults to **10 MiB** when unset.

```cue
bundle: {
  max_webhook_body_bytes: 5242880   // 5 MiB
}
```

| Value | Effect |
|---|---|
| `0` or unset | Use the default (10 MiB) |
| Any positive integer | Limit in bytes |

Raise this if you receive large monorepo push payloads. Lower it to reduce memory pressure on high-traffic gateways.
