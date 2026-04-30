# Examples

Complete bundle examples for common scenarios. All examples are also available in the [`examples/bundles/`](https://github.com/jarin-devoss/varTrack/tree/main/examples/bundles) directory.

---

## MongoDB basic

Push config from a GitHub repo to MongoDB. Branch name maps to environment.

```cue
bundle: {
  platforms: [{
    github: {
      endpoint:        "https://github.com"
      push_event_name: "push"
      pr_event_name:   "pull_request"
      secret:          "my-webhook-secret"
      token:           "ghp_xxx"
    }
  }]

  datasources: [{
    mongo: {
      endpoint:   "mongodb://mongo:27017"
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

## Config file in Git

The `file_name` in a rule points to a file inside your Git repository. varTrack parses it and writes **every key-value pair** to the sink. Example file at `configs/app.yaml`:

```yaml
# configs/app.yaml
app_name:        my-service
log_level:       info
max_connections: 100
debug:           false
```

When pushed to `main`, varTrack writes all four keys to the MongoDB collection `production-config`:

| Key | Value |
|---|---|
| `app_name` | `my-service` |
| `log_level` | `info` |
| `max_connections` | `100` |
| `debug` | `false` |

### Nested keys

Nested YAML/JSON objects are flattened with dot notation:

```yaml
database:
  host: prod-db.internal
  port: 5432
  name: myapp

server:
  port: 8080
  timeout: 30
```

Written to the sink as: `database.host`, `database.port`, `database.name`, `server.port`, `server.timeout`.

---

### Files that contain more than just config

Real-world config files often carry metadata, version info, or multiple services in the same file — things you don't want to push to your sink. Use `root_key` to extract only the relevant subtree before varTrack processes the file.

#### `package.json` — extract only the vartrack block

A Node.js project uses `package.json` as its canonical config store. The file has `name`, `version`, `scripts`, `dependencies`, etc. — but varTrack should only sync the `vartrack` block:

```json
{
  "name": "my-service",
  "version": "2.4.1",
  "scripts": { "start": "node index.js" },
  "dependencies": { "express": "^4.18.0" },
  "vartrack": {
    "database_host": "mongo.prod.internal",
    "max_connections": 50,
    "feature_dark_mode": true
  }
}
```

```cue
rules: [{
  file_name: "package.json"
  root_key:  "vartrack"    // only the inner object is flattened and written
}]
```

Keys written: `database_host`, `max_connections`, `feature_dark_mode`.
Everything outside the `vartrack` block is ignored.

---

#### Multi-service monorepo file — extract one service's block

A single `configs/services.yaml` declares config for several services:

```yaml
auth-service:
  jwt_secret:      "changeme"
  token_ttl:       3600
  refresh_enabled: true

payment-service:
  stripe_key:      "sk_live_xxx"
  webhook_secret:  "whsec_xxx"
  retry_attempts:  3

notification-service:
  smtp_host: "smtp.sendgrid.net"
  smtp_port: 587
```

Use a separate rule per service, each with its own `root_key`:

```cue
rules: [
  {
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/services.yaml"
    root_key:             "auth-service"
    destination_template: "{env}-auth"
    repositories:         ["my-org/monorepo"]
    branch_map: { main: "production", develop: "staging" }
  },
  {
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/services.yaml"
    root_key:             "payment-service"
    destination_template: "{env}-payment"
    repositories:         ["my-org/monorepo"]
    branch_map: { main: "production", develop: "staging" }
  },
]
```

The `auth-service` rule writes `jwt_secret`, `token_ttl`, `refresh_enabled` to `production-auth`.
The `payment-service` rule writes `stripe_key`, `webhook_secret`, `retry_attempts` to `production-payment`.
Neither rule sees the other service's keys.

---

#### Helm `values.yaml` — extract the app block

A `values.yaml` mixes Kubernetes deployment config with application config. Only the `app` block should be pushed to Redis:

```yaml
replicaCount: 3
image:
  repository: my-org/my-service
  tag: "1.4.2"
resources:
  limits:
    cpu: "500m"
    memory: "256Mi"

app:
  log_level:       warn
  max_connections: 200
  cache_ttl:       300
  feature_flags:
    dark_mode:    true
    new_checkout: false
```

```cue
rules: [{
  file_name: "helm/values.yaml"
  root_key:  "app"
  datasource: "redis"
  destination_template: "{env}:cfg"
}]
```

Keys written to Redis: `log_level`, `max_connections`, `cache_ttl`, `feature_flags.dark_mode`, `feature_flags.new_checkout`.
The Kubernetes fields (`replicaCount`, `image`, `resources`) are never touched.

---

### Multiple files

Each rule targets one file. To sync multiple files to the same (or different) sinks, add more rules:

```cue
rules: [
  {
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/app.yaml"
    repositories:         ["my-org/*"]
    destination_template: "{env}-app"
  },
  {
    platform:             "github"
    datasource:           "mongo"
    file_name:            "configs/database.yaml"
    repositories:         ["my-org/*"]
    destination_template: "{env}-db"
  },
]
```

---

## Multi-sink fan-out

One push writes to MongoDB, Redis, and ZooKeeper simultaneously.

```cue
bundle: {
  platforms: [{
    github: {
      endpoint: "https://github.com"
      secret:   "my-webhook-secret"
      token:    "ghp_xxx"
    }
  }]

  datasources: [
    { mongo:     { endpoint: "mongodb://mongo:27017",  database: "vartrack" } },
    { redis:     { host: "redis", port: 6379 } },
    { zookeeper: { hosts: ["zookeeper:2181"] } },
  ]

  rules: [
    {
      platform:             "github"
      datasource:           "mongo"
      file_name:            "configs/app.yaml"
      repositories:         ["my-org/*"]
      destination_template: "{env}-config"
      branch_map: { main: "production", develop: "staging" }
    },
    {
      platform:             "github"
      datasource:           "redis"
      file_name:            "configs/app.yaml"
      repositories:         ["my-org/*"]
      destination_template: "{env}:cfg"
      branch_map: { main: "production", develop: "staging" }
    },
    {
      platform:             "github"
      datasource:           "zookeeper"
      file_name:            "configs/app.yaml"
      repositories:         ["my-org/*"]
      destination_template: "/myapp/{env}"
      branch_map: { main: "production", develop: "staging" }
    },
  ]
}
```

---

## PR preview environments

Each pull request gets its own isolated collection in MongoDB.

```cue
rules: [{
  platform:             "github"
  datasource:           "mongo"
  file_name:            "configs/app.yaml"
  repositories:         ["my-org/app"]
  destination_template: "{env}-config"
  env_as_pr:            true    // PR #42 → collection "pr-42-config"
  self_heal:            false   // preview envs don't need self-heal
}]
```

---

## S3 + Linux server

Write config to S3 and also deploy a `.env` file to a Linux server.

```cue
datasources: [
  {
    s3: {
      bucket:            "my-configs"
      region:            "us-east-1"
      access_key_id:     "AKIA..."
      secret_access_key: "..."
    }
  },
  {
    linux_server: {
      host:        "server.example.com"
      username:    "deploy"
      private_key: "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
    }
  }
]

rules: [
  {
    platform:             "github"
    datasource:           "s3"
    file_name:            "configs/app.yaml"
    repositories:         ["my-org/*"]
    destination_template: "configs/{env}/"
    branch_map: { main: "production" }
  },
  {
    platform:             "github"
    datasource:           "linux_server"
    file_name:            "configs/app.yaml"
    repositories:         ["my-org/*"]
    destination_template: "/etc/app/{env}.env"
    branch_map: { main: "production" }
  }
]
```

---

## Secret value forms

Every credential field accepts one of three forms:

```cue
// 1. Plain string
token: "ghp_xxx"

// 2. Mounted file (Docker secret / K8s secret volume) — read at startup
token: {file: "/run/secrets/github-pat"}

// 3. Vault reference — fetched at ETL time
token: {ref: {path: "vartrack/github", key: "token"}}
```

---

## Vault secrets

Reference secrets from HashiCorp Vault instead of storing them in the bundle.

```cue
bundle: {
  secret_managers: [{
    vault: {
      endpoint:    "https://vault.mycompany.com"
      mount_point: "secret"
      kv_version:  2
      token_auth: {
        token: {file: "/run/secrets/vault-token"}   // vault token from a mounted file
      }
    }
  }]

  platforms: [{
    github: {
      endpoint: "https://github.com"
      secret:   {ref: {path: "vartrack/github", key: "webhook_secret"}}  // from Vault
      token:    {ref: {path: "vartrack/github", key: "token"}}
    }
  }]

  datasources: [{
    mongo: {
      endpoint: "mongodb://mongo:27017"
      database: "vartrack"
      username: "vartrack"
      password: {ref: {path: "vartrack/mongo", key: "password"}}         // from Vault
    }
  }]
}
```

---

## GitHub Enterprise

Connect to a self-hosted GitHub Enterprise instance.

```cue
platforms: [{
  github: {
    endpoint:        "https://git.mycompany.com"   // GHE URL
    protocol:        "https"
    push_event_name: "push"
    pr_event_name:   "pull_request"
    secret:          "my-webhook-secret"
    token:           "ghp_xxx"
    verify_ssl:      true
    org_name:        "my-org"
  }
}]
```

---

## CUE schema validation

Validate every config push against a per-tenant CUE schema.

```cue
// In your schema repo: schemas/myapp.cue
#Config: {
  database_host:   string
  max_connections: int & >=1 & <=1000
  log_level:       "debug" | "info" | "warn" | "error"
}
```

```cue
// In config.cue
schema_registry: {
  platform: "github"
  repo:     "my-org/schemas"
  branch:   "main"
}
```

varTrack fetches the schema, runs `cue vet`, and rejects the payload before any write if validation fails.
