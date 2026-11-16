"""
Seeds the live demo through the real paths:

  0. Web UI sign-in: an OAuth application on each Gitea server (gitea's
     credentials go to /live-state/ui-sign-in, gitea-team's client secret to
     Vault) and a read-only user
  1. Gitea: org, a config repo, a schema repo (bundle.json + CUE schema) and
     a presets repo: rule settings the bundle.json files and vartrack.json
     files share through "extends"
  1b. The config repo's vartrack.json: one on main, another on develop
  2. Schema registry warm-up for the tenant
  3. Gitea webhooks on the config repo → gateway /webhooks/{datasource}
  4. Real commits: production config on `main`, staging config on `develop`,
     then a follow-up change on `main` — each push syncs every sink
  4b. gitea-team, a second Git server: the payments org with a private repo,
      read by varTrack as vartrack-bot and synced to Redis (redis-team), and
      its own schema repo — tenant "payments", whose bundle.json extends a
      preset on gitea — and .gitea/vartrack.json in the payments repo
  5. A few direct edits in the sinks so there is drift to look at:
     MongoDB is put back by self-heal; Redis and S3 stay out of sync until
     someone syncs them from Git.

Safe to run again: repos and branches are reused, webhooks follow DATASOURCES,
and commits made in Gitea since are kept — only the header naming the sinks
is updated.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

GITEA = os.environ.get("GITEA_URL", "http://gitea:3000")
AUTH = (os.environ.get("GITEA_ADMIN_USER", "demo-admin"), os.environ.get("GITEA_ADMIN_PASSWORD", "VarTrack123"))
TEAM_GITEA = os.environ.get("TEAM_GITEA_URL", "http://gitea-team:3000")
TEAM_AUTH = (os.environ.get("TEAM_GITEA_ADMIN_USER", "team-admin"), os.environ.get("TEAM_GITEA_ADMIN_PASSWORD", "VarTrack123"))
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:5657")
ORCH = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator-api:8000")
TENANT = os.environ.get("TENANT_ID", "default")
ORG = "vartrack-demo"
CONFIG_REPO = "demo-configs"
SCHEMA_REPO = "schemas"
PRESETS_REPO = "presets"
REPO_CONFIG = "vartrack.json"
TEAM_TENANT = "payments"
CONFIG_FILE = "app-settings.yaml"
DATASOURCES = ["mongo-primary", "zookeeper", "redis", "s3-minio", "configmap-checkout", "helm-checkout"]
HERE = Path(__file__).parent
STATE = Path(os.environ.get("STATE_DIR", "/live-state"))
VAULT = os.environ.get("VAULT_ADDR", "http://vault:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "root")
# Where config.cue's ui block reads gitea-team's OAuth client secret: {ref: {path, key}}.
UI_SECRET_PATH, UI_SECRET_KEY = "ui/gitea-team", "client_secret"


@dataclass(frozen=True)
class Server:
    """A Gitea server: its platform name in the bundle, address, admin and org."""
    name: str
    url: str
    auth: tuple[str, str]
    org: str


MAIN = Server("gitea", GITEA, AUTH, ORG)
TEAM = Server("gitea-team", TEAM_GITEA, TEAM_AUTH, "payments")  # another team's Git server
TEAM_REPO = "payments-config"
TEAM_DATASOURCE = "redis-team"
BOT = ("vartrack-bot", "VarTrack123")  # how varTrack reads the private payments repository


def log(msg: str) -> None:
    print(f"[seed] {msg}", flush=True)


def wait_http(url: str, name: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=5).status_code < 500:
                log(f"{name} is up")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    sys.exit(f"{name} not reachable at {url}")


def gitea(method: str, path: str, server: Server = MAIN, **kw) -> requests.Response:
    return requests.request(method, f"{server.url}/api/v1{path}", auth=server.auth, timeout=30, **kw)


def config_file(name: str, **replace: str) -> str:
    """A config from configs/, headed with the sinks it feeds (adding a sink changes the file, so it gets synced)."""
    text = (HERE / "configs" / name).read_text()
    for old, new in replace.items():
        text = text.replace(old, new)
    return f"# Synced by varTrack to: {', '.join(DATASOURCES)}\n" + text


def update_header(branch: str) -> bool:
    """Rewrite the config's first line to list the current sinks; True when that made a commit."""
    r = gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/contents/{CONFIG_FILE}", params={"ref": branch})
    if not r.ok:
        return False
    text = base64.b64decode(r.json()["content"]).decode()
    header = f"# Synced by varTrack to: {', '.join(DATASOURCES)}"
    first, _, rest = text.partition("\n")
    body = rest if first.startswith("# Synced by varTrack to:") else text
    updated = f"{header}\n{body}"
    if updated == text:
        return False
    put_file(CONFIG_REPO, CONFIG_FILE, updated, "Update the list of sinks this config feeds", branch=branch)
    return True


def put_file(repo: str, path: str, content: str, message: str, branch: str = "main", server: Server = MAIN) -> bool:
    """Commit *content* unless it is already there; True when that made a commit."""
    existing = gitea("GET", f"/repos/{server.org}/{repo}/contents/{path}", server, params={"ref": branch})
    body = {"content": base64.b64encode(content.encode()).decode(), "message": message, "branch": branch}
    if existing.ok:
        if base64.b64decode(existing.json()["content"]).decode() == content:
            log(f"{repo}@{branch}:{path} already up to date")
            return False
        body["sha"] = existing.json()["sha"]
        r = gitea("PUT", f"/repos/{server.org}/{repo}/contents/{path}", server, json=body)
    else:
        r = gitea("POST", f"/repos/{server.org}/{repo}/contents/{path}", server, json=body)
    r.raise_for_status()
    log(f"pushed {repo}@{branch}:{path} — {message}")
    return True


def ensure_repo(name: str, server: Server = MAIN, private: bool = False) -> None:
    if gitea("GET", f"/repos/{server.org}/{name}", server).ok:
        return
    r = gitea("POST", f"/orgs/{server.org}/repos", server,
              json={"name": name, "auto_init": True, "default_branch": "main", "private": private})
    r.raise_for_status()
    log(f"created {'private ' if private else ''}repo {server.org}/{name} on {server.name}")


def put_tree(repo: str, local: Path, message: str, server: Server = MAIN) -> None:
    """Commit every file under *local* to *repo* at the same relative path."""
    for f in sorted(p for p in local.rglob("*") if p.is_file()):
        put_file(repo, f.relative_to(local).as_posix(), f.read_text(), message, server=server)


def push_repo_configs() -> None:
    """The config repo's vartrack.json on each branch that exists: main and develop differ."""
    for branch in ("main", "develop"):
        if branch == "main" or gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/branches/{branch}").ok:
            put_file(CONFIG_REPO, REPO_CONFIG, (HERE / "repo-config" / f"demo-configs.{branch}.json").read_text(),
                     f"vartrack.json for {branch}", branch=branch)


def warm_tenant(tenant: str, repo_url: str) -> None:
    warm = requests.post(f"{ORCH}/v1/tenants/{tenant}/schemas/warm", timeout=60,
                         json={"repo_url": repo_url, "branch": "main"})
    log(f"schema registry warm-up ({tenant}): {warm.status_code} {warm.text[:200]}")


def ensure_user(user: tuple[str, str], server: Server = MAIN) -> None:
    if gitea("GET", f"/users/{user[0]}", server).ok:
        return
    gitea("POST", "/admin/users", server, json={"username": user[0], "email": f"{user[0]}@example.com",
                                                "password": user[1], "must_change_password": False}).raise_for_status()
    log(f"created user {user[0]} on {server.name}")


LEDGER_REDIS = ("redis", 6379)  # the orchestrator's sync ledger, next to the Celery broker (DB 0)


def _redis(*args: str):
    """Just enough of a Redis client for LRANGE and MGET (the seed image has no redis package)."""
    import socket

    with socket.create_connection(LEDGER_REDIS, timeout=5) as sock:
        sock.sendall((f"*{len(args)}\r\n" + "".join(f"${len(a.encode())}\r\n{a}\r\n" for a in args)).encode())
        reply = sock.makefile("rb")

        def read():
            line = reply.readline().rstrip(b"\r\n")
            kind, rest = line[:1], line[1:]
            if kind == b"*":
                return None if int(rest) < 0 else [read() for _ in range(int(rest))]
            if kind == b"$":
                return None if int(rest) < 0 else reply.read(int(rest) + 2)[:-2].decode()
            if kind == b":":
                return int(rest)
            if kind == b"-":
                raise RuntimeError(rest.decode())
            return rest.decode()
        return read()


def ui_tasks() -> list[dict]:
    """The syncs the orchestrator recorded, newest first — read from its ledger, since the UI's API needs sign-in."""
    try:
        ids = _redis("LRANGE", f"vt:ui:tasks:{TENANT}", "0", "199")
        return [json.loads(raw) for raw in (_redis("MGET", *[f"vt:ui:task:{i}" for i in ids]) if ids else []) if raw]
    except Exception:
        return []


def task_ids() -> set[str]:
    return {t["taskId"] for t in ui_tasks()}


def wait_for_syncs(count: int, seen: set[str], timeout: int = 240) -> None:
    """Wait for *count* syncs that were not in *seen* to finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = [t for t in ui_tasks() if t["taskId"] not in seen]
        done = [t for t in tasks if t["state"] in ("SUCCEEDED", "FAILED")]
        if len(done) >= count and not any(t["state"] in ("PENDING", "RUNNING", "RETRYING") for t in tasks):
            failed = [t for t in done if t["state"] == "FAILED"]
            log(f"{len(done)} sync(s) finished, {len(failed)} failed")
            for t in failed:
                log(f"  failed: {t.get('rule')} {t.get('env', '')} — {t.get('message')}")
            return
        time.sleep(4)
    log(f"timed out waiting for {count} sync(s)")


# Web UI sign-in: the OAuth application and the callback of each place the UI runs.
UI_OAUTH_APP = "varTrack web UI"
UI_CALLBACKS = [f"{ui}/api/orchestrator/v1/auth/callback" for ui in ("http://localhost:5173", "http://localhost:8080")]
VIEWER = ("demo-viewer", "VarTrack123")  # can read the demo repos (they are public) but not write


def ui_sign_in() -> None:
    """Register the web UI on both Gitea servers for sign-in and hand the credentials to the orchestrator."""
    out = STATE / "ui-sign-in"
    out.mkdir(parents=True, exist_ok=True)
    body = {"name": UI_OAUTH_APP, "redirect_uris": UI_CALLBACKS, "confidential_client": True}
    for server in (MAIN, TEAM):
        apps = {a["name"]: a for a in gitea("GET", "/user/applications/oauth2", server).json()}
        if UI_OAUTH_APP in apps:
            # Gitea shows a client secret only when it issues one: updating the application issues a new one.
            r = gitea("PATCH", f"/user/applications/oauth2/{apps[UI_OAUTH_APP]['id']}", server, json=body)
        else:
            r = gitea("POST", "/user/applications/oauth2", server, json=body)
        r.raise_for_status()
        app = r.json()
        (out / f"{server.name}-client-id").write_text(app["client_id"])
        if server is TEAM:
            # The team's secret lives in Vault, to show {ref: …} next to gitea's {file: …}.
            vault_put(UI_SECRET_PATH, {UI_SECRET_KEY: app["client_secret"]})
            (out / f"{server.name}-client-secret").unlink(missing_ok=True)
        else:
            (out / f"{server.name}-client-secret").write_text(app["client_secret"])
        log(f"web UI sign-in: OAuth application {UI_OAUTH_APP!r} ready on {server.name}")
        # demo-viewer has an account on both: a reader of the demo repos, outside the payments team.
        ensure_user(VIEWER, server)


def vault_put(path: str, data: dict) -> None:
    """Write a KV v2 secret to the demo Vault (dev mode: kept in memory, gone when Vault restarts)."""
    requests.post(f"{VAULT}/v1/secret/data/{path}", headers={"X-Vault-Token": VAULT_TOKEN},
                  json={"data": data}, timeout=10).raise_for_status()
    log(f"Vault: secret/{path} written")


def payments_team() -> None:
    """gitea-team: the payments org with a private config repo, read by varTrack as a bot, synced to Redis."""
    if gitea("POST", "/orgs", TEAM, json={"username": TEAM.org, "visibility": "private"}).ok:
        log(f"created private org {TEAM.org} on {TEAM.name}")
    ensure_repo(TEAM_REPO, TEAM, private=True)
    ensure_repo(SCHEMA_REPO, TEAM, private=True)
    ensure_user(BOT, TEAM)
    for repo in (TEAM_REPO, SCHEMA_REPO):
        gitea("PUT", f"/repos/{TEAM.org}/{repo}/collaborators/{BOT[0]}", TEAM,
              json={"permission": "read"}).raise_for_status()

    # The payments tenant: its own schema repo, whose bundle.json extends a
    # preset in vartrack-demo/presets on gitea — and a preset of its own, which
    # the payments repo's .gitea/vartrack.json extends.
    put_tree(SCHEMA_REPO, HERE / "team-schemas", "The payments tenant's rules", server=TEAM)
    put_file(TEAM_REPO, f".gitea/{REPO_CONFIG}", (HERE / "repo-config" / "payments-config.json").read_text(),
             "vartrack.json for payments-config", server=TEAM)
    warm_tenant(TEAM_TENANT, f"{TEAM_GITEA}/{TEAM.org}/{SCHEMA_REPO}.git")

    url = f"{GATEWAY}/webhooks/{TEAM_DATASOURCE}"
    hooks = {h["config"]["url"] for h in gitea("GET", f"/repos/{TEAM.org}/{TEAM_REPO}/hooks", TEAM).json()}
    if url not in hooks:
        gitea("POST", f"/repos/{TEAM.org}/{TEAM_REPO}/hooks", TEAM, json={
            "type": "gitea", "active": True, "events": ["push"], "config": {"url": url, "content_type": "json"},
        }).raise_for_status()
        log(f"webhook {TEAM.name}:{TEAM.org}/{TEAM_REPO} → {url}")

    seen = task_ids()
    config = f"# Synced by varTrack to: {TEAM_DATASOURCE}\n" + (HERE / "configs" / "payments.yaml").read_text()
    if put_file(TEAM_REPO, CONFIG_FILE, config, "Configure payments-api for production", server=TEAM):
        wait_for_syncs(1, seen)


def drift() -> None:
    """Change a few values directly in the sinks, the way someone would during an incident."""
    import boto3
    import redis
    from pymongo import MongoClient

    mongo = MongoClient("mongodb://mongo:27017", serverSelectionTimeoutMS=5000)
    # mongo-primary stores one nested document per file (STRATEGY_DOCUMENT).
    res = mongo.vartrack.variables.update_one(
        {"_vt_env": "production", "_vt_datasource": "mongo-primary", "_vt_file": "app-settings.yaml"},
        {"$set": {"data.features.debug_mode": "true"}},
    )
    log(f"MongoDB: production features.debug_mode → true ({res.modified_count} doc) — self-heal should put it back")
    mongo.close()

    r = redis.Redis(host="redis", port=6379, db=2)
    hash_key = next((k for k in r.scan_iter("production:config*") if r.type(k) == b"hash" and r.hexists(k, "cache.ttl_seconds")), None)
    if hash_key:
        r.hset(hash_key, "cache.ttl_seconds", "30")
        log(f"Redis: {hash_key.decode()} cache.ttl_seconds → 30 — self-heal is off, stays out of sync")

    s3 = boto3.client("s3", endpoint_url="http://minio:9002", aws_access_key_id="minioadmin",
                      aws_secret_access_key="minioadmin", region_name="us-east-1")
    key = f"configs/{TENANT}/staging/config.json"
    try:
        data = json.loads(s3.get_object(Bucket="vartrack", Key=key)["Body"].read())
        data["api.rate_limit"] = "250"
        data["features.debug_mode"] = "false"
        s3.put_object(Bucket="vartrack", Key=key, Body=json.dumps(data, indent=2).encode(), ContentType="application/json")
        log(f"S3: {key} api.rate_limit → 250, features.debug_mode → false — stays out of sync")
    except Exception as exc:
        log(f"S3 edit skipped: {exc}")


def main() -> None:
    wait_http(f"{GITEA}/api/v1/version", "Gitea")
    wait_http(f"{TEAM_GITEA}/api/v1/version", "team Gitea")
    wait_http(f"{ORCH}/v1/health", "orchestrator")
    wait_http(f"{GATEWAY}/health/liveness", "gateway")

    ui_sign_in()

    r = gitea("POST", "/orgs", json={"username": ORG, "visibility": "public"})
    if r.ok:
        log(f"created org {ORG}")
    for name in (CONFIG_REPO, SCHEMA_REPO, PRESETS_REPO):
        ensure_repo(name)

    put_tree(PRESETS_REPO, HERE / "presets", "Shared rule settings")
    put_file(SCHEMA_REPO, "bundle.json", (HERE / "schemas" / "bundle.json").read_text(), "Rules for every sink")
    put_file(SCHEMA_REPO, "app-settings.yaml.cue", (HERE / "schemas" / "app-settings.yaml.cue").read_text(), "Schema for app-settings.yaml")
    push_repo_configs()

    warm_tenant(TENANT, f"{GITEA}/{ORG}/{SCHEMA_REPO}.git")

    wanted = {f"{GATEWAY}/webhooks/{ds}" for ds in DATASOURCES}
    hooks = {h["config"]["url"]: h["id"] for h in gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/hooks").json()}
    for url, hook_id in hooks.items():
        if url not in wanted:
            gitea("DELETE", f"/repos/{ORG}/{CONFIG_REPO}/hooks/{hook_id}").raise_for_status()
            log(f"removed webhook → {url}")
    for url in sorted(wanted - hooks.keys()):
        r = gitea("POST", f"/repos/{ORG}/{CONFIG_REPO}/hooks", json={
            "type": "gitea", "active": True, "events": ["push"],
            "config": {"url": url, "content_type": "json"},
        })
        r.raise_for_status()
        log(f"webhook → {url}")

    payments_team()

    if gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/contents/{CONFIG_FILE}").ok:
        # Already seeded: keep whatever was committed since, and only update
        # the header naming the sinks, so a new sink gets its first sync.
        seen = task_ids()
        changed = sum(update_header(branch) for branch in ("main", "develop"))
        if changed:
            wait_for_syncs(changed * len(DATASOURCES), seen)
        log("done — open the UI")
        return

    seen = task_ids()
    put_file(CONFIG_REPO, CONFIG_FILE, config_file("production.yaml"), "Configure checkout-api for production")
    if not gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/branches/develop").ok:
        gitea("POST", f"/repos/{ORG}/{CONFIG_REPO}/branches", json={"new_branch_name": "develop", "old_branch_name": "main"}).raise_for_status()
        log("created branch develop")
        push_repo_configs()
    time.sleep(2)
    put_file(CONFIG_REPO, CONFIG_FILE, config_file("staging.yaml"), "Staging: debug logging and a smaller pool", branch="develop")
    wait_for_syncs(2 * len(DATASOURCES), seen)

    prod = config_file("production.yaml", **{'rate_limit: "5000"': 'rate_limit: "6000"'})
    seen = task_ids()
    put_file(CONFIG_REPO, CONFIG_FILE, prod, "Raise the production rate limit to 6000")
    wait_for_syncs(len(DATASOURCES), seen)

    drift()
    log("done — open the UI")


if __name__ == "__main__":
    if sys.argv[1:] == ["ui-sign-in"]:
        # Only (re)register the web UI with both Gitea servers:
        #   docker compose run --rm --no-deps live-seed ui-sign-in
        wait_http(f"{GITEA}/api/v1/version", "Gitea")
        wait_http(f"{TEAM_GITEA}/api/v1/version", "team Gitea")
        ui_sign_in()
    else:
        main()
