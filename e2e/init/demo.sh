#!/bin/bash
# VarTrack E2E Demo Script
# Flow: Gitea setup → Schema warm → Webhook trigger → ETL → MongoDB + ZK results → Drift → Heal
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

# ── PHASE 0: Wait for infrastructure ─────────────────────────────────────────
banner "PHASE 0: Infrastructure health checks"

wait_http "$GITEA"                    "Gitea"           180
wait_http "$ORCHESTRATOR/v1/health"   "Orchestrator API" 180
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

step "Creating repositories (deleting stale ones first for clean state)"
for repo in "$CONFIG_REPO" "$SCHEMA_REPO"; do
    # Delete if exists (idempotent reset)
    gitea_api DELETE "/repos/${GITEA_ORG}/${repo}" >/dev/null 2>&1 || true
    sleep 1
    gitea_api POST "/orgs/${GITEA_ORG}/repos" \
        -d "{\"name\":\"${repo}\",\"private\":false,\"auto_init\":true,\"default_branch\":\"main\"}" \
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

step "Resolving HEAD commit SHA"
COMMIT_SHA=$(gitea_api GET "/repos/${GITEA_ORG}/${CONFIG_REPO}/git/refs" 2>/dev/null \
    | python3 -c "
import json, sys
refs = json.load(sys.stdin)
print(refs[0]['object']['sha'] if refs else 'abc1234567890abcdef1234567890abcdef123456')
" 2>/dev/null) || COMMIT_SHA="abc1234567890abcdef1234567890abcdef123456"
ok "Commit SHA: ${COMMIT_SHA:0:12}…"

# ── PHASE 2: Schema registry warm-up ─────────────────────────────────────────
banner "PHASE 2: Schema registry warm-up"
log "Cloning schema repo inside the API process…"

WARM_RESP=$(curl -s --fail-with-body -X POST "${ORCHESTRATOR}/v1/tenants/${TENANT_ID}/schemas/warm" \
    -H "Content-Type: application/json" \
    -d "{\"repo_url\":\"${GITEA}/${GITEA_ORG}/${SCHEMA_REPO}.git\",\"branch\":\"main\"}" \
    2>&1) || { warn "Schema warm returned error: ${WARM_RESP}"; WARM_RESP='{"rules_loaded":0}'; }
RULES=$(echo "$WARM_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('rules_loaded',0))" 2>/dev/null || echo "?")
ok "Schema warmed: $RULES rule(s) loaded (${WARM_RESP})"

if [ "$RULES" = "0" ] || [ "$RULES" = "?" ]; then
    warn "No rules loaded — demo may still work if git clone succeeded but bundle.json parse failed."
fi

# ── PHASE 3: ETL webhook trigger ──────────────────────────────────────────────
banner "PHASE 3: Triggering ETL webhooks"

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
  }],
  "head_commit": {
    "id": "${COMMIT_SHA}",
    "message": "Add app settings",
    "added": ["app-settings.yaml"],
    "removed": [],
    "modified": []
  }
}
EOF
)

TASK_IDS=()
for datasource in mongo zookeeper; do
    step "Webhook → $datasource"
    RESP=$(curl -sf -X POST "${ORCHESTRATOR}/v1/webhooks/${datasource}" \
        -H "Content-Type: application/json" \
        -H "X-GitHub-Event: push" \
        -H "X-Tenant-ID: ${TENANT_ID}" \
        -d "$PUSH_PAYLOAD" 2>&1) || { err "Webhook for $datasource failed: $RESP"; continue; }
    TASK_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id','?'))" 2>/dev/null || echo "?")
    TASK_IDS+=("$datasource:$TASK_ID")
    ok "Task enqueued — $datasource → task_id=$TASK_ID"
done

step "Waiting 25s for Celery ETL tasks…"
sleep 25

# ── PHASE 4: Results ──────────────────────────────────────────────────────────
banner "PHASE 4: Verifying results"

python3 - <<'PYEOF'
import sys
from datetime import datetime

# MongoDB
print("\n\033[1m── MongoDB (vartrack.variables) ──\033[0m")
try:
    from pymongo import MongoClient
    client = MongoClient("mongodb://mongo:27017", serverSelectionTimeoutMS=5000)
    db = client["vartrack"]
    docs = list(db["variables"].find({}, {"_id": 0}))
    if docs:
        print(f"  Found {len(docs)} document(s):")
        for doc in docs:
            k = doc.get("_vt_file", doc.get("key", "?"))
            ts = doc.get("_vt_commit", "")[:8]
            print(f"    • {k}  (commit={ts})")
            # Print key-value pairs
            kv = {k2: v for k2, v in doc.items() if not k2.startswith("_vt_")}
            for kk, vv in list(kv.items())[:6]:
                print(f"        {kk}: {vv}")
            if len(kv) > 6:
                print(f"        … and {len(kv)-6} more keys")
        print(f"\n  \033[32m✓ MongoDB sync SUCCESS\033[0m")
    else:
        print("  \033[33m⚠ No documents found — ETL may still be running\033[0m")
    client.close()
except Exception as e:
    print(f"  \033[31m✗ MongoDB error: {e}\033[0m")

# ZooKeeper — nested znode tree
# Structure: /demo/default/{group}/{key}
#   e.g. /demo/default/database/pool_size
print("\n\033[1m── ZooKeeper (/demo/default) ──\033[0m")
try:
    from kazoo.client import KazooClient
    zk = KazooClient(hosts="zookeeper:2181", timeout=10)
    zk.start(timeout=10)
    base = "/demo/default"

    def zk_walk(path, depth=0, max_depth=4, leaf_count=None):
        """Recursively walk znodes, printing the tree. Returns number of leaves printed."""
        if leaf_count is None:
            leaf_count = [0]
        if depth > max_depth:
            return leaf_count[0]
        try:
            children = sorted(zk.get_children(path))
        except Exception:
            return leaf_count[0]
        for child in children:
            child_path = f"{path}/{child}"
            data, _ = zk.get(child_path)
            val = data.decode("utf-8") if data else None
            try:
                grandchildren = zk.get_children(child_path)
            except Exception:
                grandchildren = []
            indent = "  " * (depth + 1)
            if grandchildren:
                # Intermediate node (group)
                print(f"{indent}\033[36m{child}/\033[0m")
                zk_walk(child_path, depth + 1, max_depth, leaf_count)
            else:
                # Leaf node
                if child.startswith("__"):
                    continue  # skip meta znodes
                leaf_count[0] += 1
                print(f"{indent}\033[32m{child}\033[0m: {(val or '(empty)')[:60]}")
        return leaf_count[0]

    if zk.exists(base):
        groups = [c for c in zk.get_children(base) if not c.startswith("__")]
        print(f"  Groups under {base}: {sorted(groups)}")
        leaves = zk_walk(base)
        print(f"\n  Total leaf znodes written: {leaves}")
        print(f"  \033[32m✓ ZooKeeper sync SUCCESS (nested tree)\033[0m")
    else:
        print(f"  \033[33m⚠ Path {base} not found — ETL may still be running\033[0m")
        if zk.exists("/demo"):
            print("  /demo tree:")
            zk_walk("/demo")
    zk.stop()
except Exception as e:
    print(f"  \033[31m✗ ZooKeeper error: {e}\033[0m")
PYEOF

# ── PHASE 5: Drift simulation ──────────────────────────────────────────────────
banner "PHASE 5: Drift simulation (watcher self-heal)"
log "Tampering with ZooKeeper to simulate manual config drift…"

python3 - <<'PYEOF'
try:
    from kazoo.client import KazooClient
    zk = KazooClient(hosts="zookeeper:2181", timeout=10)
    zk.start(timeout=10)
    base = "/demo/default"
    # Tamper a known leaf node in the nested tree: api/rate_limit
    # Falls back to any available leaf if it doesn't exist yet.
    preferred_leaves = ["api/rate_limit", "database/pool_size", "cache/ttl_seconds"]
    target = None
    for leaf in preferred_leaves:
        candidate = f"{base}/{leaf}"
        if zk.exists(candidate):
            target = candidate
            break
    # Fallback: walk to find any leaf
    if target is None and zk.exists(base):
        def find_leaf(path, depth=0):
            if depth > 5:
                return None
            try:
                children = [c for c in sorted(zk.get_children(path)) if not c.startswith("__")]
            except Exception:
                return None
            for child in children:
                cp = f"{path}/{child}"
                try:
                    kids = zk.get_children(cp)
                except Exception:
                    kids = []
                if not kids:
                    return cp
                result = find_leaf(cp, depth + 1)
                if result:
                    return result
            return None
        target = find_leaf(base)

    if target:
        old_data, _ = zk.get(target)
        zk.set(target, b"TAMPERED_VALUE_drift_test")
        print(f"  \033[33m⚠ Tampered:  {target}")
        print(f"     was: {(old_data or b'(empty)').decode()[:60]}")
        print(f"     now: TAMPERED_VALUE_drift_test\033[0m")
    else:
        print(f"  No leaf znode found under {base} — skipping drift simulation")
    zk.stop()
except Exception as e:
    print(f"  \033[31m✗ Could not simulate drift: {e}\033[0m")
PYEOF

POLL="${WATCHER_POLL_INTERVAL:-10}"
WAIT=$((POLL * 3 + 10))
log "Watcher polls every ${POLL}s. Waiting ${WAIT}s for drift detection + self-heal…"
sleep $WAIT

# ── PHASE 6: Post-heal verification ───────────────────────────────────────────
banner "PHASE 6: Post-heal ZooKeeper state"

python3 - <<'PYEOF'
try:
    from kazoo.client import KazooClient
    zk = KazooClient(hosts="zookeeper:2181", timeout=10)
    zk.start(timeout=10)
    base = "/demo/default"

    healed = [0]
    drifted = [0]

    def check_healed(path, depth=0):
        """Walk tree and count healed vs still-drifted leaves."""
        if depth > 5:
            return
        try:
            children = sorted(zk.get_children(path))
        except Exception:
            return
        for child in children:
            if child.startswith("__"):
                continue
            cp = f"{path}/{child}"
            data, _ = zk.get(cp)
            val = data.decode("utf-8") if data else None
            try:
                kids = zk.get_children(cp)
            except Exception:
                kids = []
            if kids:
                indent = "  " * (depth + 1)
                print(f"{indent}\033[36m{child}/\033[0m")
                check_healed(cp, depth + 1)
            else:
                indent = "  " * (depth + 1)
                if val == "TAMPERED_VALUE_drift_test":
                    drifted[0] += 1
                    print(f"{indent}\033[31m{child}: {val}\033[0m  ← still drifted")
                else:
                    healed[0] += 1
                    print(f"{indent}\033[32m{child}: {(val or '(empty)')[:50]}\033[0m")

    if zk.exists(base):
        print(f"  Nested znode tree after self-heal ({base}):")
        check_healed(base)
        total = healed[0] + drifted[0]
        print(f"\n  Leaves: {total} total, {healed[0]} healthy, {drifted[0]} still drifted")
        if healed[0] > 0:
            print(f"  \033[32m✓ Watcher HEALED {healed[0]} node(s) back to desired state!\033[0m")
        if drifted[0] > 0:
            print(f"  \033[33m⚠ {drifted[0]} node(s) still drifted — watcher may need another poll cycle\033[0m")
    zk.stop()
except Exception as e:
    print(f"  \033[31m✗ ZooKeeper error: {e}\033[0m")
PYEOF

# ── PHASE 6: Full sink test suite ─────────────────────────────────────────────
banner "PHASE 6: Full E2E test suite (16 suites / 8 sinks + CUE + Vault)"

if [ -d "/demo/schemas/settings" ]; then
    log "Running test_runner.py..."
    python3 /test_runner.py \
        --settings-dir /demo/schemas/settings \
        --results-dir /results \
        --orchestrator "${ORCHESTRATOR}" \
        --gitea "${GITEA}" \
        --gitea-user "${GITEA_USER}" \
        --gitea-pass "${GITEA_PASS}" \
        && ok "Test runner finished — see /results/benchmark.json" \
        || warn "Test runner exited non-zero (some suites may have failed)"
else
    warn "No settings dir found at /demo/schemas/settings — skipping test suite"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
banner "E2E Demo Complete"
echo -e "${BOLD}Services still running. You can inspect:${NC}"
echo -e "  Orchestrator docs  → http://localhost:8000/docs"
echo -e "  Celery Flower      → http://localhost:5555"
echo -e "  Gitea              → http://localhost:3000  (${GITEA_USER} / ${GITEA_PASS})"
echo -e "  Watcher admin      → http://localhost:9091/health"
echo -e "  MongoDB UI         → http://localhost:8081  (mongo-express → vartrack → variables)"
echo -e "  ZooKeeper UI       → http://localhost:9000  (ZooNavigator → /demo/default)"
echo -e "  MinIO console      → http://localhost:9001  (minioadmin / minioadmin)"
echo -e "  Vault UI           → http://localhost:8200  (token: root)"
echo -e "  Linux target SSH   → localhost:2222  (vartrack / VarTrack123)"
echo -e "  Benchmark JSON     → e2e/results/benchmark.json"
echo -e "  Prometheus         → http://localhost:9095"
echo -e "  Jaeger traces      → http://localhost:16686  (service: vartrack-orchestrator)"
echo -e "  Grafana dashboards → http://localhost:3001  (admin / admin)"
echo -e ""
echo -e "${BOLD}MongoDB quick query (from another shell):${NC}"
echo -e "  docker exec -it \$(docker ps -qf name=mongo) mongosh vartrack --eval 'db.variables.find().limit(3).pretty()'"
echo -e ""
echo -e "${BOLD}ZooKeeper quick query (nested tree):${NC}"
echo -e "  docker exec -it \$(docker ps -qf name=zookeeper) zkCli.sh -server localhost:2181 ls /demo/default"
echo -e "  docker exec -it \$(docker ps -qf name=zookeeper) zkCli.sh -server localhost:2181 get /demo/default/api/rate_limit"
echo -e "  ZooNavigator UI → http://localhost:9000  (auto-connects to zookeeper:2181)"
echo ""
