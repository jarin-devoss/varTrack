# Bundle Reference

All three varTrack services read a single shared CUE bundle (`config.cue`). The bundle declares your platforms, datasources, routing rules, schema registry, and secret managers.

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
    verify_ssl:      true
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
    update_strategy: "STRATEGY_KEY_VALUE"      // or "STRATEGY_DOCUMENT"
  }
}]
```

### Redis

```cue
datasources: [{
  redis: {
    host:     "redis"
    port:     6379
    password: "secret"
    db:       0
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

Rules map a platform + file combination to a datasource and define how data is written.

```cue
rules: [{
  platform:             "github"              // matches platform name (or "github-{tag}")
  datasource:           "mongo"               // matches datasource name (or "mongo-{tag}")
  file_name:            "configs/app.yaml"    // file path in the repo to watch
  repositories:         ["my-org/*"]          // glob patterns — which repos trigger this

  // Where data lands
  destination_template: "{env}-config"

  // Source (pick one)
  file_name:     "configs/app.yaml"           // single file tracked in all repos
  // file_path_map: { production: "configs/prod.yaml", staging: "configs/staging.yaml" }

  // Repository filtering
  exclude_repositories: ["my-org/legacy-.*"] // negative glob patterns
  // repository_overrides: { "my-org/special": { enabled: false } }

  // Root key extraction (optional)
  root_key: "vartrack"                        // extract subtree before flattening
                                              // set to "" to process the whole file

  // Variable overlay (optional)
  variables_map: {
    REGION: "us-east-1"                       // inject or override any key
  }

  // Environment resolution
  branch_map: {
    main:    "production"
    develop: "staging"
  }
  env_as_pr:     false
  env_as_branch: false
  env_as_tags:   false

  // Sync behavior
  sync_mode:     "AUTO"                       // AUTO, GIT_UPSERT_ALL, GIT_SMART_REPAIR, LIVE_STATE
  prune:         false                        // delete keys no longer in Git
  prune_last:    false                        // defer deletion until all sources processed
  prune_protection: ["SYSTEM_*", "_vt_*"]    // glob patterns for keys that must never be deleted

  // Drift detection
  self_heal:     true                         // auto-repair on drift
}]
```

---

## Per-repo overrides

Individual repositories can override a subset of their rule settings without touching `config.cue`. Add a `vartrack.json` file at the root of the repository:

```json
{
  "root_key": "app_config",
  "branch_map": { "main": "production", "staging": "staging" },
  "prune": true
}
```

Keys in `vartrack.json` are merged on top of the central rule for that repository on every webhook. Infrastructure keys (`platform`, `datasource`, `token`, `repositories`, `self_heal`, …) are always taken from the bundle and cannot be overridden. The schema registry is unaffected.

See the full [Per-Repo Overrides reference](per-repo-overrides.md) for the complete list of overridable keys, security model, and troubleshooting.

---

## `schema_registry`

Points varTrack to a Git repo containing CUE schemas for validation:

```cue
schema_registry: {
  platform: "github"
  repo:     "my-org/schemas"
  branch:   "main"
}
```

When a webhook arrives, varTrack validates the parsed config against the tenant's CUE schema before writing anything. Invalid payloads are rejected with a descriptive error.

The schema repo also holds `bundle.json` (or `rules.json`) — a rule manifest that lets Celery workers resolve rule configuration locally without a round-trip to the API process. See the [Schema Registry reference](schema-registry.md) for the full repo structure, `bundle.json` format, tenant env vars, and caching behaviour.

---

## `secret_managers`

Secrets referenced in the bundle (tokens, passwords) can be resolved from HashiCorp Vault:

```cue
secret_managers: [{
  vault: {
    endpoint:    "https://vault.mycompany.com"
    mount_point: "secret"
    kv_version:  2

    // Auth — pick one:
    token_auth: {
      token: "hvs.xxxx"
    }
    // approle_auth: {
    //   role_id:   "role-id"
    //   secret_id: "secret-id"
    // }
    // kubernetes_auth: {
    //   role:            "vartrack"
    //   service_account: "/var/run/secrets/kubernetes.io/serviceaccount/token"
    // }
    // userpass_auth: {
    //   username: "vartrack"
    //   password: "pass"
    // }
  }
}]
```

---

## Secret value forms

Every credential field in the bundle (`token`, `secret`, `password`, `private_key`, …) accepts three forms:

```cue
// 1. Plain string — value written directly in the bundle
token: "ghp_xxx"

// 2. File reference — read from a file at service startup (whitespace stripped)
//    Ideal for Docker secrets, Kubernetes secret mounts, or any on-disk secret
token: {file: "/run/secrets/github-token"}

// 3. Vault reference — fetched from HashiCorp Vault at ETL time
//    Requires a secret_managers block in the bundle
token: {ref: {path: "vartrack/github", key: "token"}}
```

### `{file:}` — mounted secret files

The `{file:}` form keeps secrets completely out of the bundle. Mount the file via Docker or Kubernetes and point to it:

```cue
secret_managers: [{
  vault: {
    endpoint:    "https://vault.mycompany.com"
    mount_point: "secret"
    kv_version:  2
    token_auth: {
      token: {file: "/run/secrets/vault-token"}   // Docker secret or K8s secret volume
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
  watcher_state_datasource:            "redis"        // watcher baseline state store
  watcher_leader_election_datasource:  "redis"        // distributed leader election (Redis or ZooKeeper)
}
```

In a single-Redis setup, all four can point to the same datasource. In production you may want separate instances for isolation. The `watcher_leader_election_datasource` also accepts `"zookeeper"`.

See [Drift Detection](../concepts/drift-detection.md) for how the watcher uses these.

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
