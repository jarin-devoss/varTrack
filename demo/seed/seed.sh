#!/bin/bash
# varTrack demo seed
#
# Seeds the demo once: Gitea (the organisation, the config and schema repositories, the file and the bundle), the web UI's
# sign-in (accounts, access, the OAuth application), the schema warm-up, and the first sync of each rule. It runs no test.
#
# A second run finds the demo seeded and leaves it as it is, so what a person pushed since stays. `docker compose down -v`
# starts again from nothing.
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()    { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
ok()     { echo -e "${GREEN}  ✓${NC} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()    { echo -e "${RED}  ✗${NC} $*"; }
banner() { echo -e "\n${BOLD}${CYAN}╔══════════════════════════════════════╗${NC}"; \
           echo -e "${BOLD}${CYAN}║  $* ${NC}"; \
           echo -e "${BOLD}${CYAN}╚══════════════════════════════════════╝${NC}\n"; }
step()   { echo -e "\n${BOLD}${BLUE}── $* ──────────────────────────────────${NC}"; }

# ── Config ────────────────────────────────────────────────────────────────────
ORCHESTRATOR=${ORCHESTRATOR_URL:-http://orchestrator-api:8000}
GITEA=${GITEA_URL:-http://gitea:3000}
GITEA_USER=${GITEA_ADMIN_USER:-demo-admin}
GITEA_PASS=${GITEA_ADMIN_PASSWORD:-VarTrack123}
GITEA_ORG="vartrack-demo"
CONFIG_REPO="demo-configs"
SCHEMA_REPO="schemas"
TENANT_ID="demo"
SEEDED="${DEMO_STATE:-/demo-state}/seeded"

# The rules that get a first sync: every rule of config.cue but redis-string, which is left out of the tenant's bundle.json
# on purpose: it is the refused rule that step 7 of the tour fixes. mongo and zookeeper heal themselves (self_heal in
# config.cue); the others do not.
DATASOURCES="mongo mongo-file zookeeper redis-hash linux_server s3"

# A push to the config repository syncs it, through a Gitea webhook per rule (created in the last phase), to the gateway.
GATEWAY=${GATEWAY_URL:-http://gateway:5657}

# The orchestrator accepts a call only with a signed grant (INTERNAL_AUTH_SECRET): this script plays the
# gateway's part. With no secret the orchestrator runs open and nothing is sent.
auth_header() {
    if [ -n "${INTERNAL_AUTH_SECRET:-}" ]; then
        echo "Authorization: Bearer $(python3 /grant.py mint --audience sync --subject demo-seed --platform gitea --datasource "${1:-}")"
    else
        echo "X-E2E-Auth: none"
    fi
}

# ── Helpers ───────────────────────────────────────────────────────────────────
wait_http() {
    local url=$1 label=$2 max=${3:-120} n=0
    log "Waiting for $label at $url ..."
    until curl -sf --max-time 3 "$url" >/dev/null 2>&1; do
        [ $n -ge $max ] && { err "$label not ready after ${max}s"; exit 1; }
        sleep 3; n=$((n+3))
        printf "."
    done
    echo ""
    ok "$label ready"
}

wait_tcp() {
    local host=$1 port=$2 label=$3 max=${4:-60} n=0
    log "Waiting for $label ($host:$port) ..."
    until (echo >/dev/tcp/$host/$port) 2>/dev/null; do
        [ $n -ge $max ] && { warn "$label TCP not ready after ${max}s (continuing)"; return 0; }
        sleep 2; n=$((n+2)); printf "."
    done
    echo ""; ok "$label TCP ready"
}

gitea_api() {
    local method=$1 path=$2; shift 2
    curl -sf -X "$method" "${GITEA}/api/v1${path}" \
        -u "${GITEA_USER}:${GITEA_PASS}" \
        -H "Content-Type: application/json" "$@"
}

push_file() {
    # Push a file to Gitea via API (base64 encoded).
    local repo=$1 filepath=$2 content_b64=$3 message=$4
    # Try create first, fall back to update (needs SHA for update).
    local existing
    existing=$(gitea_api GET "/repos/${GITEA_ORG}/${repo}/contents/${filepath}" 2>/dev/null || echo "")
    if [ -n "$existing" ]; then
        local sha; sha=$(echo "$existing" | python3 -c "import json,sys; print(json.load(sys.stdin)['sha'])" 2>/dev/null || echo "")
        gitea_api PUT "/repos/${GITEA_ORG}/${repo}/contents/${filepath}" \
            -d "{\"message\":\"${message}\",\"content\":\"${content_b64}\",\"sha\":\"${sha}\"}" >/dev/null
    else
        gitea_api POST "/repos/${GITEA_ORG}/${repo}/contents/${filepath}" \
            -d "{\"message\":\"${message}\",\"content\":\"${content_b64}\"}" >/dev/null
    fi
}

# Wait until a task has finished (its own state, SUCCEEDED or FAILED) and print that state; TIMEOUT after $2 seconds.
wait_task() {
    local datasource=$1 task_id=$2 max=${3:-120} n=0 state=""
    while [ $n -lt "$max" ]; do
        state=$(curl -s --max-time 10 -H "$(auth_header "$datasource")" "${ORCHESTRATOR}/v1/tasks/${task_id}" \
            | python3 -c "
import json, sys
try:
    result = json.load(sys.stdin).get('result')
except ValueError:
    result = None
print(result.get('state', '') if isinstance(result, dict) else '')" 2>/dev/null || echo "")
        if [ "$state" = "SUCCEEDED" ] || [ "$state" = "FAILED" ]; then echo "$state"; return 0; fi
        sleep 2; n=$((n+2))
    done
    echo "TIMEOUT"
}

ready() {
    echo -e "\n${BOLD}The demo is ready.${NC}"
    echo -e "  Web UI   → http://localhost:8080   sign in with Gitea as demo-admin / VarTrack123"
    echo -e "  Gitea    → http://localhost:3000   ${GITEA_USER} / ${GITEA_PASS}  (the config repository is ${GITEA_ORG}/${CONFIG_REPO})"
    echo -e "  Accounts → demo-admin (everything), demo-writer (pushes, not to main), demo-viewer (reads, dry runs),"
    echo -e "             demo-outsider (sees nothing). Every password is VarTrack123."
    echo -e "  Rules    → six sync; gitea/redis-string is refused on purpose (step 7 of the tour)."
    echo -e "  Next     → demo/README.md: the tour, and how to stop and reset it."
}

# ── PHASE 0: Wait for infrastructure ─────────────────────────────────────────
banner "PHASE 0: Infrastructure health checks"

wait_http "$GITEA"                    "Gitea"           180
wait_http "$ORCHESTRATOR/v1/health"   "Orchestrator API" 180

# Seeded once: when the marker is there and both repositories are too, there is nothing to do, and what a person
# has pushed since stays. (A marker without its repositories, or repositories without the marker, is a half-seeded
# demo, and is seeded again.)
if [ -f "$SEEDED" ] \
    && gitea_api GET "/repos/${GITEA_ORG}/${CONFIG_REPO}" >/dev/null 2>&1 \
    && gitea_api GET "/repos/${GITEA_ORG}/${SCHEMA_REPO}" >/dev/null 2>&1; then
    ok "The demo is already seeded ($(cat "$SEEDED")): leaving it as it is"
    ready
    exit 0
fi

wait_tcp  redis   6379                "Redis"            60
wait_tcp  mongo   27017               "MongoDB"          60
wait_tcp  zookeeper 2181              "ZooKeeper"        60

log "Giving Celery worker a moment to connect..."
sleep 5

# ── PHASE 1: Gitea setup ──────────────────────────────────────────────────────
banner "PHASE 1: Gitea setup"

step "Creating organisation: $GITEA_ORG"
gitea_api POST "/orgs" \
    -d "{\"username\":\"${GITEA_ORG}\",\"visibility\":\"public\",\"repo_admin_change_team_access\":true}" \
    >/dev/null 2>&1 && ok "Organisation created" || warn "Organisation may already exist"

step "Creating repositories (a half-seeded demo's are deleted first, so the seed starts clean)"
# The config repository is private, so an account with no access to it (demo-outsider) sees none of the demo's
# rules; the schema repository stays public (the services read it without credentials).
for repo in "$CONFIG_REPO" "$SCHEMA_REPO"; do
    private=false; [ "$repo" = "$CONFIG_REPO" ] && private=true
    gitea_api DELETE "/repos/${GITEA_ORG}/${repo}" >/dev/null 2>&1 || true
    sleep 1
    gitea_api POST "/orgs/${GITEA_ORG}/repos" \
        -d "{\"name\":\"${repo}\",\"private\":${private},\"auto_init\":true,\"default_branch\":\"main\"}" \
        >/dev/null 2>&1 && ok "Repo ${GITEA_ORG}/${repo} created" || warn "Repo ${repo} creation may have failed"
done
sleep 2  # give Gitea a moment after repo creation

step "Pushing config file: app-settings.yaml → $CONFIG_REPO"
CONFIG_B64=$(base64 -w 0 /demo/configs/app-settings.yaml)
push_file "$CONFIG_REPO" "app-settings.yaml" "$CONFIG_B64" "Add app settings" \
    && ok "app-settings.yaml pushed" || warn "File push returned non-200 (may still work)"

step "Pushing schema: bundle.json → $SCHEMA_REPO"
BUNDLE_B64=$(base64 -w 0 /demo/schemas/bundle.json)
push_file "$SCHEMA_REPO" "bundle.json" "$BUNDLE_B64" "Add bundle.json" \
    && ok "bundle.json pushed" || warn "File push returned non-200 (may still work)"

step "Setting up web UI sign-in: users, access, protected main, OAuth application"
python3 /ui_sign_in.py && ok "web UI sign-in ready" || { err "web UI sign-in setup failed"; exit 1; }

step "Resolving HEAD commit SHA"
COMMIT_SHA=$(gitea_api GET "/repos/${GITEA_ORG}/${CONFIG_REPO}/git/refs" 2>/dev/null \
    | python3 -c "
import json, sys
refs = json.load(sys.stdin)
print(refs[0]['object']['sha'] if refs else 'abc1234567890abcdef1234567890abcdef123456')
" 2>/dev/null) || COMMIT_SHA="abc1234567890abcdef1234567890abcdef123456"

# ── PHASE 2: Schema registry warm-up ─────────────────────────────────────────
banner "PHASE 2: Schema registry warm-up"

WARM_RESP=$(curl -s --fail-with-body -X POST "${ORCHESTRATOR}/v1/tenants/${TENANT_ID}/schemas/warm" \
    -H "Content-Type: application/json" \
    -H "$(auth_header)" \
    2>&1) || { warn "Schema warm returned error: ${WARM_RESP}"; WARM_RESP='{"rules_loaded":0}'; }
RULES=$(echo "$WARM_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('rules_loaded',0))" 2>/dev/null || echo "?")

if [ "$RULES" = "0" ] || [ "$RULES" = "?" ]; then
    err "The schema registry loaded no rules (${WARM_RESP}): the demo would have nothing to sync"
    exit 1
fi
ok "Rules loaded: ${RULES}"

# ── PHASE 3: The first sync of each rule ─────────────────────────────────────
banner "PHASE 3: The first sync of each rule"

PUSH_PAYLOAD=$(cat <<EOF
{
  "ref": "refs/heads/main",
  "before": "0000000000000000000000000000000000000000",
  "after": "${COMMIT_SHA}",
  "repository": {
    "id": 1,
    "name": "${CONFIG_REPO}",
    "full_name": "${GITEA_ORG}/${CONFIG_REPO}",
    "owner": {"name": "${GITEA_ORG}", "login": "${GITEA_ORG}"},
    "clone_url": "${GITEA}/${GITEA_ORG}/${CONFIG_REPO}.git",
    "html_url": "${GITEA}/${GITEA_ORG}/${CONFIG_REPO}",
    "default_branch": "main"
  },
  "pusher": {"name": "demo-user", "email": "demo@example.com"},
  "commits": [{
    "id": "${COMMIT_SHA}",
    "message": "Add app settings",
    "added": ["app-settings.yaml"],
    "removed": [],
    "modified": []
  }]
}
EOF
)

TASKS=()
for datasource in $DATASOURCES; do
    step "Webhook → $datasource"
    RESP=$(curl -sf -X POST "${ORCHESTRATOR}/v1/webhooks/${datasource}" \
        -H "Content-Type: application/json" \
        -H "X-Gitea-Event: push" \
        -H "$(auth_header "$datasource")" \
        -d "$PUSH_PAYLOAD" 2>&1) || { err "Webhook for $datasource failed: $RESP"; exit 1; }
    TASK_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id','?'))" 2>/dev/null || echo "?")
    TASKS+=("$datasource:$TASK_ID")
    ok "Task enqueued — $datasource → task_id=$TASK_ID"
done

step "Waiting for each sync to finish"
for entry in "${TASKS[@]}"; do
    datasource=${entry%%:*}; task_id=${entry#*:}
    state=$(wait_task "$datasource" "$task_id" 120)
    if [ "$state" = "SUCCEEDED" ]; then
        ok "$datasource synced"
    else
        err "$datasource: the first sync ended ${state} (task ${task_id}): the demo is not ready"
        exit 1
    fi
done

# ── PHASE 4: Webhooks, so a push to the config repository syncs it ───────────
banner "PHASE 4: Webhooks"
for datasource in $DATASOURCES; do
    url="${GATEWAY}/webhooks/${datasource}"
    hook="{\"type\":\"gitea\",\"active\":true,\"events\":[\"push\"],\"config\":{\"url\":\"${url}\",\"content_type\":\"json\"}}"
    if gitea_api POST "/repos/${GITEA_ORG}/${CONFIG_REPO}/hooks" -d "$hook" >/dev/null; then
        ok "webhook → ${url}"
    else
        err "the webhook for ${datasource} was not created"; exit 1
    fi
done

# A push to the schema repository refreshes the tenant's rules at once (otherwise after SCHEMA_TTL_SECONDS, five minutes):
# step 7 of the tour pushes an entry to bundle.json there.
url="${GATEWAY}/webhooks/schema-registry"
hook="{\"type\":\"gitea\",\"active\":true,\"events\":[\"push\"],\"config\":{\"url\":\"${url}\",\"content_type\":\"json\"}}"
if gitea_api POST "/repos/${GITEA_ORG}/${SCHEMA_REPO}/hooks" -d "$hook" >/dev/null; then
    ok "webhook → ${url}"
else
    err "the webhook for the schema repository was not created"; exit 1
fi

mkdir -p "$(dirname "$SEEDED")"
date -u +"seeded %Y-%m-%dT%H:%M:%SZ" > "$SEEDED"

ready
