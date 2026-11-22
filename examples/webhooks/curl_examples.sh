#!/usr/bin/env bash
# Send the webhooks a Git platform would send, by hand: to try a rule, or a gateway.
#
# The gateway serves POST /webhooks/{datasource}: the datasource names the rule, with
# the platform the event headers say sent it. With APP_ENV=production it checks
# each delivery's signature against the platform's `secret` in config.cue (401 when
# it does not match) and refuses a delivery id it has seen before (401). Outside
# production it checks neither.
#
# To sync a file without a push, use the CLI instead:
#   vt sync --file configs/app.yaml --datasource mongo --env staging --dry-run
#
#   GATEWAY_URL=https://vartrack.example.com SECRET=my-webhook-secret ./curl_examples.sh
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:5657}"
SECRET="${SECRET:-my-webhook-secret}"   # the platform's `secret` in config.cue

# A push to main of acme-corp/configs. The orchestrator reads the files the rule
# names at commit `after`, from the repository at clone_url.
PAYLOAD='{"ref":"refs/heads/main","after":"0123456789abcdef0123456789abcdef01234567","repository":{"full_name":"acme-corp/configs","clone_url":"https://github.com/acme-corp/configs.git"},"head_commit":{"id":"0123456789abcdef0123456789abcdef01234567","message":"update app config"}}'

# HMAC-SHA256 of the exact bytes sent, in hex.
sign() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //'; }
delivery() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }   # a fresh id per delivery

# ── 1. GitHub: a push to the rule for the "mongo" datasource ──────────────────
# X-Hub-Signature-256 is "sha256=" and the hex digest.
echo "--- GitHub push → /webhooks/mongo"
curl -sS -X POST "${GATEWAY_URL}/webhooks/mongo" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -H "X-GitHub-Delivery: $(delivery)" \
  -H "X-Hub-Signature-256: sha256=$(sign "$PAYLOAD")" \
  --data-binary "$PAYLOAD"
echo   # 202 {"task_id": "…", "message": "…"}

# ── 2. GitHub: a tagged datasource ("mongo-primary") ──────────────────────────
echo "--- GitHub push → /webhooks/mongo-primary"
curl -sS -X POST "${GATEWAY_URL}/webhooks/mongo-primary" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -H "X-GitHub-Delivery: $(delivery)" \
  -H "X-Hub-Signature-256: sha256=$(sign "$PAYLOAD")" \
  --data-binary "$PAYLOAD"
echo

# ── 3. Gitea: the same push; the signature is the hex digest with no prefix ───
echo "--- Gitea push → /webhooks/redis-config"
curl -sS -X POST "${GATEWAY_URL}/webhooks/redis-config" \
  -H "Content-Type: application/json" \
  -H "X-Gitea-Event: push" \
  -H "X-Gitea-Delivery: $(delivery)" \
  -H "X-Gitea-Signature: $(sign "$PAYLOAD")" \
  --data-binary "$PAYLOAD"
echo

# ── 4. An event that is not a push or a pull request is ignored ───────────────
PING='{"zen":"Keep it logically awesome.","hook_id":1}'
echo "--- GitHub ping → 200 event ignored"
curl -sS -X POST "${GATEWAY_URL}/webhooks/mongo" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -H "X-GitHub-Delivery: $(delivery)" \
  -H "X-Hub-Signature-256: sha256=$(sign "$PING")" \
  --data-binary "$PING"
echo

# ── 5. Health ─────────────────────────────────────────────────────────────────
echo "--- gateway health"
curl -sS "${GATEWAY_URL}/health/liveness"; echo
curl -sS "${GATEWAY_URL}/health/readiness"; echo   # 200 once the orchestrator answers
