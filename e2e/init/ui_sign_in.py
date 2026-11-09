#!/usr/bin/env python3
"""Set the demo up for signing in to the web UI as different people.

Run by demo.sh after the repositories hold their first commit. It creates:

  demo-writer    can push to the demo's config repository, except to its protected branch (main)
  demo-viewer    can read it, nothing more
  demo-outsider  an account that can read none of the demo's repositories (they are private)

gives them those rights, protects main (only demo-admin pushes to it), and registers the web UI as an OAuth
application on Gitea. The application's client id and secret go to $DEMO_STATE/ui-sign-in/, which the services
read through `{file: …}` in config.cue (the same way the live stack does it).

Idempotent: running it again keeps the users and issues a new client secret.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

GITEA = os.environ.get("GITEA_URL", "http://gitea:3000").rstrip("/")
ADMIN = (os.environ.get("GITEA_ADMIN_USER", "demo-admin"), os.environ.get("GITEA_ADMIN_PASSWORD", "VarTrack123"))
STATE = Path(os.environ.get("DEMO_STATE", "/demo-state")) / "ui-sign-in"
ORG, CONFIG_REPO, SCHEMA_REPO = "vartrack-demo", "demo-configs", "schemas"
PASSWORD = "VarTrack123"

# Where the UI runs: the UI image on :8080 (CI and the demo stack), the dev server on :5173.
CALLBACKS = [f"{ui}/api/orchestrator/v1/auth/callback" for ui in ("http://localhost:8080", "http://localhost:5173")]
APP_NAME = "varTrack web UI"

USERS = {  # name → what it may do in the config repository
    "demo-writer": "write",
    "demo-viewer": "read",
    "demo-outsider": None,
}


def api(method: str, path: str, **kw) -> requests.Response:
    return requests.request(method, f"{GITEA}/api/v1{path}", auth=ADMIN, timeout=20, **kw)


def log(message: str) -> None:
    print(f"  ui-sign-in: {message}", flush=True)


def ensure_user(name: str) -> None:
    r = api("POST", "/admin/users", json={
        "username": name, "password": PASSWORD, "email": f"{name}@vartrack.local", "must_change_password": False})
    if r.status_code not in (201, 409, 422):      # 422: it exists already
        raise RuntimeError(f"could not create {name}: {r.status_code} {r.text[:160]}")


def grant(name: str, permission: str | None) -> None:
    if permission is None:
        return
    r = api("PUT", f"/repos/{ORG}/{CONFIG_REPO}/collaborators/{name}", json={"permission": permission})
    if r.status_code not in (204, 200):
        raise RuntimeError(f"could not give {name} {permission} on {CONFIG_REPO}: {r.status_code} {r.text[:160]}")


def protect_main() -> None:
    """Only demo-admin pushes to main: a writer's Sync of the environment main feeds is refused by Git's rules."""
    body = {
        "branch_name": "main", "rule_name": "main",
        "enable_push": True, "enable_push_whitelist": True, "push_whitelist_usernames": [ADMIN[0]],
        "required_approvals": 0,
    }
    r = api("POST", f"/repos/{ORG}/{CONFIG_REPO}/branch_protections", json=body)
    if r.status_code in (409, 422):                # it exists: bring it to the same shape
        r = api("PATCH", f"/repos/{ORG}/{CONFIG_REPO}/branch_protections/main", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"could not protect main: {r.status_code} {r.text[:160]}")


def register_app() -> None:
    body = {"name": APP_NAME, "redirect_uris": CALLBACKS, "confidential_client": True}
    apps = {a["name"]: a for a in api("GET", "/user/applications/oauth2").json()}
    if APP_NAME in apps:
        # Gitea shows a client secret only when it issues one: updating the application issues a new one.
        r = api("PATCH", f"/user/applications/oauth2/{apps[APP_NAME]['id']}", json=body)
    else:
        r = api("POST", "/user/applications/oauth2", json=body)
    r.raise_for_status()
    app = r.json()
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "gitea-client-id").write_text(app["client_id"])
    (STATE / "gitea-client-secret").write_text(app["client_secret"])


def main() -> int:
    for name, permission in USERS.items():
        ensure_user(name)
        grant(name, permission)
        log(f"{name}: {permission or 'no access to the demo repositories'}")
    protect_main()
    log("main is protected: only demo-admin pushes to it")
    register_app()
    log(f"the web UI is registered for sign-in (callbacks: {', '.join(CALLBACKS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
