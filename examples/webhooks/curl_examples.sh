#!/usr/bin/env bash
# VarTrack webhook examples — trigger ETL from the command line.
#
# The {datasource} path segment routes the push to the matching rule in the bundle.
# Each datasource configured in bundle.datasources gets its own webhook URL.
#
# Replace:
#   GATEWAY_URL — your gateway service address
#   HMAC_SECRET — the webhook secret configured in your GitHub/GitLab settings
#   PAYLOAD     — a real GitHub/GitLab push event body (or use the minimal example below)

GATEWAY_URL="http://localhost:5657"

# ── Minimal GitHub push event payload ─────────────────────────────────────────
PAYLOAD='{
  "ref": "refs/heads/main",
  "repository": {
    "name": "my-config-repo",
    "full_name": "my-org/my-config-repo",
    "clone_url": "https://github.com/my-org/my-config-repo.git"
  },
  "commits": [
    {
      "id": "abc1234567890",
      "message": "chore: update app config",
      "added":    ["configs/app.yaml"],
      "modified": ["configs/database.toml"],
      "removed":  []
    }
  ],
  "head_commit": {
    "id": "abc1234567890"
  }
}'

# ── 1. Trigger sync to the default "mongo" datasource ─────────────────────────
echo "--- Push to mongo ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/mongo" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 2. Trigger sync to "mongo-primary" (tagged datasource) ────────────────────
echo "--- Push to mongo-primary ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/mongo-primary" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 3. Trigger sync to "mongo-dr" ─────────────────────────────────────────────
echo "--- Push to mongo-dr ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/mongo-dr" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 4. Trigger sync to ZooKeeper ──────────────────────────────────────────────
echo "--- Push to zookeeper ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/zookeeper" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 5. Trigger sync to Redis config store ─────────────────────────────────────
echo "--- Push to redis-cfg ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/redis-cfg" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 6. Trigger sync to S3 ─────────────────────────────────────────────────────
echo "--- Push to s3 ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/s3" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 7. Trigger sync to Linux server ───────────────────────────────────────────
echo "--- Push to linux_server ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/linux_server" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d "${PAYLOAD}"

# ── 8. Push bundle config to orchestrator directly ────────────────────────────
echo "--- Push bundle ---"
curl -s -X POST "${GATEWAY_URL}/v1/bundles" \
  -H "Content-Type: application/json" \
  -d @../bundles/01_mongodb_basic.cue

# ── 9. Health check ───────────────────────────────────────────────────────────
echo "--- Gateway health ---"
curl -s "${GATEWAY_URL}/healthz"

# ── 10. GitLab push event (different event header) ────────────────────────────
GITLAB_PAYLOAD='{
  "ref": "refs/heads/main",
  "project": {
    "name": "my-config-repo",
    "http_url": "https://gitlab.acme.internal/platform/my-config-repo.git"
  },
  "commits": [
    {
      "id": "abc1234567890",
      "message": "chore: update config",
      "added":    ["configs/app.yaml"],
      "modified": [],
      "removed":  []
    }
  ]
}'

echo "--- GitLab push to redis-cfg ---"
curl -s -X POST "${GATEWAY_URL}/v1/webhooks/redis-cfg" \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Event: Push Hook" \
  -d "${GITLAB_PAYLOAD}"
