# GitHub Webhook Setup

## Configure a webhook in your GitHub repo

1. Go to **Settings → Webhooks → Add webhook**
2. Set **Payload URL** to one of:
   - `https://your-gateway.example.com/v1/webhooks/mongo`
   - `https://your-gateway.example.com/v1/webhooks/zookeeper`
   - `https://your-gateway.example.com/v1/webhooks/mongo-primary`
   - (one webhook per datasource you want to sync on push)
3. Set **Content type** to `application/json`
4. Set **Secret** — copy the value to your gateway env: `WEBHOOK_SECRET=<value>`
5. Select **Just the push event**
6. Click **Add webhook**

## Multiple datasources = multiple webhooks

If you want a single push to write to both MongoDB primary and DR:

- Add two webhooks pointing to:
  - `https://your-gateway/v1/webhooks/mongo-primary`
  - `https://your-gateway/v1/webhooks/mongo-dr`

Both webhooks fire in parallel on each push. VarTrack processes them independently.

## HMAC signature verification

The gateway verifies the `X-Hub-Signature-256` header on every request.
Set the same secret in both GitHub and the gateway:

```bash
# In your gateway .env or container environment:
WEBHOOK_SECRET=your-shared-secret-here
```

## Branch filtering

Rules can be scoped to specific branches using `branch_env_map` in settings:

```json
{
  "branch_env_map": {
    "main":    "production",
    "staging": "staging",
    "dev":     "development"
  }
}
```

Pushes to branches not in the map are silently ignored.
