"""
Seeds the live demo through the real paths:

  1. Gitea: org, a config repo and a schema repo (bundle.json + CUE schema)
  2. Schema registry warm-up for the tenant
  3. Gitea webhooks on the config repo → gateway /webhooks/{datasource}
  4. Real commits: production config on `main`, staging config on `develop`,
     then a follow-up change on `main` — each push syncs every sink
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
from pathlib import Path

import requests

GITEA = os.environ.get("GITEA_URL", "http://gitea:3000")
AUTH = (os.environ.get("GITEA_ADMIN_USER", "demo-admin"), os.environ.get("GITEA_ADMIN_PASSWORD", "VarTrack123"))
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:5657")
ORCH = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator-api:8000")
TENANT = os.environ.get("TENANT_ID", "default")
ORG = "vartrack-demo"
CONFIG_REPO = "demo-configs"
SCHEMA_REPO = "schemas"
CONFIG_FILE = "app-settings.yaml"
DATASOURCES = ["mongo", "zookeeper", "redis", "s3-minio", "linux_server", "configmap", "helm"]
HERE = Path(__file__).parent
STATE = Path(os.environ.get("STATE_DIR", "/live-state"))


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


def gitea(method: str, path: str, **kw) -> requests.Response:
    return requests.request(method, f"{GITEA}/api/v1{path}", auth=AUTH, timeout=30, **kw)


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


def put_file(repo: str, path: str, content: str, message: str, branch: str = "main") -> None:
    existing = gitea("GET", f"/repos/{ORG}/{repo}/contents/{path}", params={"ref": branch})
    body = {"content": base64.b64encode(content.encode()).decode(), "message": message, "branch": branch}
    if existing.ok:
        if base64.b64decode(existing.json()["content"]).decode() == content:
            log(f"{repo}@{branch}:{path} already up to date")
            return
        body["sha"] = existing.json()["sha"]
        r = gitea("PUT", f"/repos/{ORG}/{repo}/contents/{path}", json=body)
    else:
        r = gitea("POST", f"/repos/{ORG}/{repo}/contents/{path}", json=body)
    r.raise_for_status()
    log(f"pushed {repo}@{branch}:{path} — {message}")


def ensure_repo(name: str) -> None:
    if gitea("GET", f"/repos/{ORG}/{name}").ok:
        return
    r = gitea("POST", f"/orgs/{ORG}/repos", json={"name": name, "auto_init": True, "default_branch": "main", "private": False})
    r.raise_for_status()
    log(f"created repo {ORG}/{name}")


def known_hosts() -> None:
    """Record the SSH target's host key so the Linux sink can verify it."""
    import paramiko

    STATE.mkdir(parents=True, exist_ok=True)
    t = paramiko.Transport(("linux-target", 22))
    try:
        t.start_client(timeout=15)
        key = t.get_remote_server_key()
    finally:
        t.close()
    (STATE / "known_hosts").write_text(f"linux-target {key.get_name()} {key.get_base64()}\n")
    log("recorded linux-target host key")


def ui_tasks() -> list[dict]:
    try:
        return requests.get(f"{ORCH}/v1/ui/tasks", params={"limit": 200}, headers={"X-Tenant-ID": TENANT}, timeout=10).json()["tasks"]
    except Exception:
        return []


def wait_for_syncs(count: int, timeout: int = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = ui_tasks()
        done = [t for t in tasks if t["state"] in ("SUCCEEDED", "FAILED")]
        if len(done) >= count and not any(t["state"] in ("PENDING", "RUNNING", "RETRYING") for t in tasks):
            failed = [t for t in done if t["state"] == "FAILED"]
            log(f"{len(done)} sync(s) finished, {len(failed)} failed")
            for t in failed:
                log(f"  failed: {t.get('rule')} {t.get('env', '')} — {t.get('message')}")
            return
        time.sleep(4)
    log(f"timed out waiting for {count} syncs ({len(ui_tasks())} recorded)")


def drift() -> None:
    """Change a few values directly in the sinks, the way someone would during an incident."""
    import boto3
    import redis
    from pymongo import MongoClient

    mongo = MongoClient("mongodb://mongo:27017", serverSelectionTimeoutMS=5000)
    res = mongo.vartrack.variables.update_one(
        {"key": "features.debug_mode", "_vt_env": "production"}, {"$set": {"value": "true"}}
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
    wait_http(f"{ORCH}/v1/health", "orchestrator")
    wait_http(f"{GATEWAY}/health/liveness", "gateway")

    known_hosts()

    r = gitea("POST", "/orgs", json={"username": ORG, "visibility": "public"})
    if r.ok:
        log(f"created org {ORG}")
    for name in (CONFIG_REPO, SCHEMA_REPO):
        ensure_repo(name)

    put_file(SCHEMA_REPO, "bundle.json", (HERE / "schemas" / "bundle.json").read_text(), "Rules for every sink")
    put_file(SCHEMA_REPO, "app-settings.yaml.cue", (HERE / "schemas" / "app-settings.yaml.cue").read_text(), "Schema for app-settings.yaml")

    warm = requests.post(f"{ORCH}/v1/tenants/{TENANT}/schemas/warm", timeout=60,
                         json={"repo_url": f"{GITEA}/{ORG}/{SCHEMA_REPO}.git", "branch": "main"})
    log(f"schema registry warm-up: {warm.status_code} {warm.text[:200]}")

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

    if gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/contents/{CONFIG_FILE}").ok:
        # Already seeded: keep whatever was committed since, and only update
        # the header naming the sinks, so a new sink gets its first sync.
        before = len(ui_tasks())
        changed = sum(update_header(branch) for branch in ("main", "develop"))
        if changed:
            wait_for_syncs(before + changed * len(DATASOURCES))
        log("done — open the UI")
        return

    before = len(ui_tasks())
    put_file(CONFIG_REPO, CONFIG_FILE, config_file("production.yaml"), "Configure checkout-api for production")
    if not gitea("GET", f"/repos/{ORG}/{CONFIG_REPO}/branches/develop").ok:
        gitea("POST", f"/repos/{ORG}/{CONFIG_REPO}/branches", json={"new_branch_name": "develop", "old_branch_name": "main"}).raise_for_status()
        log("created branch develop")
    time.sleep(2)
    put_file(CONFIG_REPO, CONFIG_FILE, config_file("staging.yaml"), "Staging: debug logging and a smaller pool", branch="develop")
    wait_for_syncs(before + 2 * len(DATASOURCES))

    prod = config_file("production.yaml", **{'rate_limit: "5000"': 'rate_limit: "6000"'})
    before = len(ui_tasks())
    put_file(CONFIG_REPO, CONFIG_FILE, prod, "Raise the production rate limit to 6000")
    wait_for_syncs(before + len(DATASOURCES))

    drift()
    log("done — open the UI")


if __name__ == "__main__":
    main()
