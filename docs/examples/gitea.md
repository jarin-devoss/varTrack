# Gitea platform example

[Gitea](https://about.gitea.com/) is a self-hosted Git service. varTrack supports it natively — the payload format is identical to GitHub and the ETL pipeline is shared; only the webhook headers differ.

| Header | Value |
|---|---|
| `X-Gitea-Event` | `push` / `pull_request` |
| `X-Gitea-Signature` | HMAC-SHA256 hex digest (no `sha256=` prefix) |
| `X-Gitea-Delivery` | Unique delivery UUID (replay protection) |

---

## Full bundle

```cue
bundle: {
  platforms: [{
    gitea: {
      endpoint:    "https://gitea.mycompany.com"
      protocol:    "https"
      token:       "gta_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"  // personal access token
      secret:      "my-webhook-secret"                // HMAC signing secret
      org_name:    "my-org"
      verify_ssl:  true
      timeout:     "30s"
      max_retries: 3
      page_size:   50
    }
  }]

  datasources: [
    { mongo: { endpoint: "mongodb://mongo:27017", database: "vartrack" } },
    { redis: { tag: "broker",  hosts: ["redis:6379"], database: 0 } },
    { redis: { tag: "results", hosts: ["redis:6379"], database: 1 } },
  ]

  rules: [{
    platform:             "gitea"
    datasource:           "mongo"
    file_name:            "configs/app.yaml"
    repositories:         ["my-org/*"]
    destination_template: "{env}-config"
    sync_mode:            "AUTO"
    self_heal:            true
    branch_map: {
      main:    "production"
      develop: "staging"
    }
    prune: {
      last:    true
      dry_run: false
    }
  }]

  global_tags: {
    celery_broker_datasource:  "redis-broker"
    celery_backend_datasource: "redis-results"
  }
}
```

---

## Webhook setup in Gitea

1. Open your repository → **Settings** → **Webhooks** → **Add Webhook** → **Gitea**
2. Set **Payload URL** to `http://gateway:5657/webhooks/mongo`
3. Set **Content type** to `application/json`
4. Set **Secret** to the same value as `secret` in your bundle
5. Under **Trigger On**, select **Push Events** and **Pull Request Events**
6. Click **Add Webhook**

Gitea will send a test ping — the gateway will reject it gracefully (no `X-Gitea-Event: push` header) and return `400`. That's expected.

---

## Using Vault secrets for the token

Keep the Gitea token out of the bundle file by referencing it from Vault:

```cue
gitea: {
  endpoint: "https://gitea.mycompany.com"
  token:    {ref: {path: "vartrack/gitea", key: "token"}}           // resolved from Vault at startup
  secret:   {ref: {path: "vartrack/gitea", key: "webhook_secret"}}
}
```

Vault setup:

```bash
vault kv put secret/vartrack/gitea \
  token=gta_xxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  webhook_secret=my-webhook-secret
```

---

## Differences from GitHub

| | GitHub | Gitea |
|---|---|---|
| Signature header | `X-Hub-Signature-256: sha256=<hex>` | `X-Gitea-Signature: <hex>` |
| Signature format | `sha256=` prefix + hex | raw hex (no prefix) |
| Delivery header | `X-GitHub-Delivery` | `X-Gitea-Delivery` |
| API base path | `/api/v3` (GHE) or `api.github.com` | `/api/v1` |
| Token auth header | `Authorization: Bearer <token>` | `Authorization: token <token>` |
| Payload structure | GitHub format | GitHub-compatible (same fields) |
