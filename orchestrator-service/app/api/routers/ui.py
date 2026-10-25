"""
app/api/routers/ui.py
──────────────────────
Read API for the web UI, mounted under /v1/ui.

  GET  /ui/rules                                      every rule with its environments
  GET  /ui/rules/{platform}/{datasource}              one rule
  GET  /ui/rules/{platform}/{datasource}/targets/{env}/keys
                                                      current vs desired, key by key
  POST /ui/rules/{platform}/{datasource}/sync         re-sync environments from Git
  GET  /ui/events?rule=&limit=                        newest first
  GET  /ui/tasks?rule=&limit=                         newest first
  GET  /ui/tasks/{task_id}
  GET  /ui/bundle                                     platforms, datasources (no credentials)

Every tenant is covered at once; the tenant is shown on each rule, event and
task, not chosen. ?tenant=
(or the X-Tenant-ID header) narrows an answer to one tenant — a filter, never a
grant. A rule is identified by its tenant, platform and datasource; a rule
endpoint without ?tenant= takes the only visible tenant that has the rule and
answers 409 when several do.

With single sign-on on (UI_AUTH_PLATFORM, see app/ui/sessions.py) every
endpoint needs a session, and answers only with what the Git platform lets
the signed-in user see (app/ui/access.py). A rule, environment or task the
user cannot see answers 404 exactly like one that does not exist.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.ui import access, ledger, scm, sessions, views

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


class SyncRequest(BaseModel):
    envs: list[str] = Field(default_factory=list)
    dryRun: bool = False
    prune: bool = False


def _all_tenants() -> list[str]:
    """Every registered tenant; UI_DEFAULT_TENANT (else "default") when none is."""
    try:
        from app.schema_registry.manager import get_schema_manager
        ids = get_schema_manager().list_tenant_ids()
        if ids:
            return sorted(ids)
    except Exception:
        pass
    return [os.getenv("UI_DEFAULT_TENANT") or "default"]


def _tenants(request: Request, tenant: str | None = None) -> list[str]:
    """The tenants an answer covers: the one asked for (?tenant= or the
    X-Tenant-ID header), else every registered tenant. A filter, never a grant:
    what the viewer may see is decided per rule (app/ui/access.py)."""
    asked = tenant or request.headers.get("x-tenant-id")
    return [asked] if asked else _all_tenants()


SCHEMA_CHECK_SECONDS = 30.0
_schema_checked_at: dict[str, float] = {}
_schema_lock = threading.Lock()


def _load_schema(mgr, tenant: str) -> None:
    try:
        mgr.get_or_clone(tenant)  # clones on first use, fetches after SCHEMA_TTL_SECONDS
    except Exception as exc:
        logger.warning("ui: schema registry for tenant=%s unavailable: %s", tenant, exc)


def _rules(tenant: str) -> list[dict]:
    from app.schema_registry.manager import get_schema_manager
    mgr = get_schema_manager()
    rules = mgr.get_all_rules(tenant)
    if not rules:
        # Nothing cloned yet: this request has to wait for it.
        _load_schema(mgr, tenant)
        return mgr.get_all_rules(tenant)
    # Loaded: answer now. The git fetch that comes due every SCHEMA_TTL_SECONDS
    # runs behind the request instead of inside it.
    now = time.time()
    with _schema_lock:
        due = now - _schema_checked_at.get(tenant, 0.0) >= SCHEMA_CHECK_SECONDS
        if due:
            _schema_checked_at[tenant] = now
    if due:
        threading.Thread(target=_load_schema, args=(mgr, tenant), name="ui_schema", daemon=True).start()
    return rules


def warm_up() -> None:
    """Compare every target once at startup, so the first page served is not the one that waits on every sink."""
    try:
        from app.schema_registry.manager import get_schema_manager
        tenants = get_schema_manager().list_tenant_ids() or [os.getenv("UI_DEFAULT_TENANT") or "default"]
        for tenant in tenants:
            views.compare_all(_rules(tenant), tenant, ledger.list_targets(tenant))
    except Exception:
        logger.warning("ui: warm-up failed", exc_info=True)


def _viewer(request: Request) -> access.Viewer:
    try:
        return access.viewer(request.cookies.get(sessions.SESSION_COOKIE))
    except access.Unauthenticated as exc:
        raise HTTPException(status_code=401, detail="sign in required") from exc
    except sessions.AuthConfigError as exc:
        # Misconfigured sign-in stays closed rather than falling back to an open UI.
        logger.error("ui auth: %s", exc)
        raise HTTPException(status_code=503, detail="sign-in is misconfigured; the orchestrator's log says why") from exc


def _find_rule(tenant: str, platform: str, datasource: str) -> dict:
    for rule in _rules(tenant):
        if rule.get("platform") == platform and rule.get("datasource") == datasource:
            return rule
    raise HTTPException(status_code=404, detail=f"no rule {platform}/{datasource} for tenant {tenant!r}")


def _visible_rule(viewer: access.Viewer, tenant: str, platform: str, datasource: str,
                  records: list[dict]) -> tuple[dict, scm.Permission]:
    rule = _find_rule(tenant, platform, datasource)
    perm = viewer.rule_permission(rule, records)
    if not perm.read:
        raise HTTPException(status_code=404, detail=f"no rule {platform}/{datasource} for tenant {tenant!r}")
    return rule, perm


def _visible(viewer: access.Viewer, tenant: str) -> tuple[list[tuple[dict, scm.Permission]], list[dict]]:
    """The rules the viewer can see (with what they may do) and the environments they can see."""
    records = ledger.list_targets(tenant)
    visible = viewer.visible_rules(_rules(tenant), records)
    by_datasource = {rule.get("datasource"): rule for rule, _ in visible}
    shown = [rec for rec in records if rec.get("datasource") in by_datasource
             and viewer.can_see_target(by_datasource[rec["datasource"]], rec)]
    return visible, shown


def _visible_names(viewer: access.Viewer, tenant: str) -> set[str] | None:
    """Names of the rules the viewer can see; None when everything is visible."""
    if not viewer.authenticated:
        return None
    return {views.rule_name(rule) for rule, _ in _visible(viewer, tenant)[0]}


def _visible_everywhere(viewer: access.Viewer, tenants: list[str]) -> list[tuple[str, list[tuple[dict, scm.Permission]], list[dict]]]:
    """Per tenant: the rules the viewer can see (with what they may do) and the
    environments they can see. A tenant with no rule the viewer can see is left
    out, so it does not appear anywhere in what they are shown."""
    def one(tenant: str):
        visible, records = _visible(viewer, tenant)
        return tenant, visible, records

    if len(tenants) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(tenants)), thread_name_prefix="ui_tenant") as pool:
            found = list(pool.map(one, tenants))
    else:
        found = [one(t) for t in tenants]
    return [f for f in found if f[1]]


def _resolve_rule(request: Request, viewer: access.Viewer, platform: str, datasource: str, tenant: str | None,
                  ) -> tuple[str, dict, scm.Permission, list[dict]]:
    """The visible rule for platform/datasource: in the tenant asked for, else in
    the only visible tenant that has it — (tenant, rule, permission, the tenant's
    environments). 404 when none does (a rule the viewer cannot see answers like
    one that does not exist); 409 when several do, naming the tenants."""
    found = []
    for t in _tenants(request, tenant):
        records = ledger.list_targets(t)
        try:
            rule, perm = _visible_rule(viewer, t, platform, datasource, records)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue
        found.append((t, rule, perm, records))
    if not found:
        raise HTTPException(status_code=404, detail=f"no rule {platform}/{datasource}")
    if len(found) > 1:
        names = ", ".join(t for t, *_ in found)
        raise HTTPException(status_code=409, detail=f"rule {platform}/{datasource} exists in tenants {names}: say which with ?tenant=")
    return found[0]


def _rules_per_platform(tenants: list[str]) -> dict[str, int]:
    """How many rules of *tenants* each platform holds — for "connect GitHub to see 3 more"."""
    counts: dict[str, int] = {}
    for tenant in tenants:
        for rule in _rules(tenant):
            platform = rule.get("platform", "")
            counts[platform] = counts.get(platform, 0) + 1
    return counts


def _hidden(viewer: access.Viewer, tenants: list[str]) -> list[dict]:
    """Rules the viewer cannot see for want of a connection: those on a platform
    that sign-in covers and this session has not connected, counted per platform.
    Only counts, no names or tenants (what /auth/session already says). Rules on
    a connected platform the viewer cannot read are not counted: that would show
    that a private repository's rules exist."""
    if not viewer.authenticated or getattr(viewer, "is_admin", False):
        return []
    cfg = sessions.config()
    if cfg is None:
        return []
    connected = set(getattr(viewer, "identities", {}))
    return [{"platform": p, "rules": n} for p, n in sorted(_rules_per_platform(tenants).items())
            if p in cfg.platforms and p not in connected]


@router.get("/rules")
def list_rules(request: Request, tenant: str | None = Query(None),
               viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    tenants = _tenants(request, tenant)
    rules: list[dict] = []
    for t, visible, records in _visible_everywhere(viewer, tenants):
        tasks = ledger.list_tasks(t, limit=200)
        views.compare_all([rule for rule, _ in visible], t, records)
        rules += [{**views.rule_view(rule, t, records, tasks), "canSync": perm.write} for rule, perm in visible]
    hidden = _hidden(viewer, tenants)
    return {"rules": rules, **({"hidden": hidden} if hidden else {})}


@router.get("/rules/{platform}/{datasource}")
def get_rule(request: Request, platform: str, datasource: str, tenant: str | None = Query(None),
             viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    tenant, rule, perm, records = _resolve_rule(request, viewer, platform, datasource, tenant)
    records = [r for r in records if r.get("datasource") == datasource and viewer.can_see_target(rule, r)]
    return {**views.rule_view(rule, tenant, records, ledger.list_tasks(tenant, limit=200)), "canSync": perm.write}


@router.get("/rules/{platform}/{datasource}/targets/{env}/keys")
def list_keys(request: Request, platform: str, datasource: str, env: str, tenant: str | None = Query(None),
              viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    tenant, rule, _, _ = _resolve_rule(request, viewer, platform, datasource, tenant)
    record = ledger.get_target(tenant, datasource, env)
    if record is None or not viewer.can_see_target(rule, record):
        raise HTTPException(status_code=404, detail=f"{env} has not been synced yet")
    try:
        return {"keys": views.key_entries(rule, tenant, record)}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _branch_for(rule: dict, env: str, record: dict | None) -> str | None:
    # The ledger's ref is the branch for a push, but a full ref for a tag or a
    # pull request and "cli/{env}" for `vt sync`: only a branch can be re-read.
    ref = (record or {}).get("ref") or ""
    if ref and not ref.startswith(("refs/", "cli/")):
        return ref
    for branch, mapped in (rule.get("branch_map") or {}).items():
        if mapped == env:
            return branch
    if rule.get("branch_as_env"):
        return env
    return None


@router.post("/rules/{platform}/{datasource}/sync")
def sync_rule(request: Request, platform: str, datasource: str, body: SyncRequest, tenant: str | None = Query(None),
              viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    """Re-read the rule's file from Git at the branch head and write it to each environment."""
    from app.pipeline.git_extractor import GitExtractor
    from app.tasks.etl import process_webhook_task

    tenant, rule, perm, records = _resolve_rule(request, viewer, platform, datasource, tenant)
    # A dry run writes nothing; a sync writes to the sinks and needs write access in Git.
    if not body.dryRun and not perm.write:
        raise HTTPException(status_code=403, detail="syncing needs write access to the rule's repository")
    mine = [r for r in records if r.get("datasource") == datasource]
    hidden = {r["env"] for r in mine if not viewer.can_see_target(rule, r)}
    envs = body.envs or [r["env"] for r in mine if r["env"] not in hidden]
    for env in envs:
        if env in hidden:
            raise HTTPException(status_code=404, detail=f"{env} has not been synced yet")
    if not envs:
        raise HTTPException(status_code=400, detail="no environments to sync — push to Git first")

    rule_config = dict(rule)
    if body.dryRun:
        rule_config["dry_run"] = True
    if body.prune:
        rule_config["prune"] = True

    extractor = GitExtractor()
    task_ids: list[str] = []
    for env in envs:
        record = ledger.get_target(tenant, datasource, env)
        branch = _branch_for(rule, env, record)
        if not branch:
            raise HTTPException(status_code=400, detail=f"can't tell which branch feeds {env}")
        repo_url = (record or {}).get("repoUrl") or rule.get("repo_url", "")
        try:
            sha = extractor.get_commit_sha(repo_url, branch, token=rule.get("token"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"can't read {branch} from Git: {exc}") from exc
        task = process_webhook_task.apply_async(
            kwargs={
                "platform": platform,
                "datasource": datasource,
                "raw_payload": json.dumps({
                    "ref": f"refs/heads/{branch}",
                    "after": sha,
                    "repository": {"full_name": repo_url},
                    "commits": [],
                }),
                "headers": {ledger.TRIGGER_HEADER: "ui"},
                "received_at": time.time(),
                "rule_config": rule_config,
                "tenant_id": tenant,
                "dry_run": body.dryRun,
            },
            queue="sync",
        )
        task_ids.append(task.id)
    return {"taskIds": task_ids}


@router.get("/events")
def list_events(request: Request, rule: str | None = None, limit: int = Query(200, ge=1, le=1000),
                tenant: str | None = Query(None), viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    events: list[dict] = []
    for t in _tenants(request, tenant):
        names = _visible_names(viewer, t)
        found = ledger.list_events(t, rule=rule, limit=limit if names is None else ledger.MAX_EVENTS)
        events += [{**e, "tenant": t} for e in found if names is None or e.get("rule") in names]
    events.sort(key=lambda e: e.get("time") or "", reverse=True)
    return {"events": events[:limit]}


@router.get("/tasks")
def list_tasks(request: Request, rule: str | None = None, limit: int = Query(100, ge=1, le=500),
               tenant: str | None = Query(None), viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    tasks: list[dict] = []
    for t in _tenants(request, tenant):
        names = _visible_names(viewer, t)
        found = ledger.list_tasks(t, rule=rule, limit=limit if names is None else ledger.MAX_TASKS)
        tasks += [{**views.public_task(task), "tenant": t} for task in found if names is None or task.get("rule") in names]
    tasks.sort(key=lambda task: task.get("createdAt") or "", reverse=True)
    return {"tasks": tasks[:limit]}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    task = ledger.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="no such task")
    if viewer.authenticated:
        names = _visible_names(viewer, task.get("tenant") or "default")
        if task.get("rule") not in (names or set()):
            raise HTTPException(status_code=404, detail="no such task")
    return {**views.public_task(task), "tenant": task.get("tenant") or "default"}


# ── Bundle ────────────────────────────────────────────────────────────────────

_bundle_cache: tuple[float, dict] | None = None


def _cue_bundle() -> dict:
    global _bundle_cache
    if _bundle_cache and time.time() - _bundle_cache[0] < 60:
        return _bundle_cache[1]
    from app.config import _load_cue_bundle, settings
    bundle = _load_cue_bundle(settings.CONFIG_PATH) if settings.CONFIG_PATH else {}
    _bundle_cache = (time.time(), bundle or {})
    return _bundle_cache[1]


_SECRET_WORDS = ("password", "secret", "token", "key", "credential")
_URL_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^/@\s]+@", re.IGNORECASE)


def _redact(value: str) -> str:
    """'mongodb://admin:pw@mongo:27017' → 'mongodb://***@mongo:27017'."""
    return _URL_USERINFO.sub(r"\g<scheme>***@", value)


def _public_detail(cfg: dict) -> dict[str, str]:
    """Connection settings worth showing; anything credential-like is left out or masked."""
    out: dict[str, str] = {}
    for k, v in cfg.items():
        if any(w in k.lower() for w in _SECRET_WORDS) or isinstance(v, (dict, list)) and k != "hosts":
            continue
        out[k] = _redact(", ".join(map(str, v)) if isinstance(v, list) else str(v))
    return out


def _endpoint(cfg: dict) -> str:
    for k in ("endpoint", "endpoint_url", "mongo_uri"):
        if cfg.get(k):
            url = str(cfg[k])
            return url.split("@", 1)[1] if "@" in url else url
    if cfg.get("hosts"):
        return ", ".join(cfg["hosts"])
    if cfg.get("host"):
        return f"{cfg['host']}:{cfg.get('port', '')}".rstrip(":")
    if cfg.get("bucket"):
        return f"s3://{cfg['bucket']}"
    return cfg.get("namespace", "") or ""


@router.get("/bundle")
def get_bundle(request: Request, tenant: str | None = Query(None),
               viewer: access.Viewer = Depends(_viewer)) -> dict[str, Any]:
    bundle = _cue_bundle()
    seen_in = _visible_everywhere(viewer, _tenants(request, tenant))
    # (tenant, rule) of every rule the viewer can see.
    rules = [(t, rule) for t, visible, _ in seen_in for rule, _ in visible]

    platforms = []
    for wrapper in bundle.get("platforms", []):
        for kind, cfg in wrapper.items():
            name = f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind
            platforms.append({
                "name": name,
                "endpoint": cfg.get("endpoint", ""),
                "pushEvent": cfg.get("push_event_name", "push"),
                "prEvent": cfg.get("pr_event_name", "pull_request"),
            })

    used_by: dict[str, list[dict[str, str]]] = {}
    for t, rule in rules:
        used_by.setdefault(rule.get("datasource", ""), []).append({"tenant": t, "name": views.rule_name(rule)})

    datasources = []
    seen: set[str] = set()
    for wrapper in bundle.get("datasources", []):
        for kind, cfg in wrapper.items():
            name = f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind
            seen.add(name)
            if viewer.authenticated and name not in used_by:
                continue  # only the sinks behind rules this user can see
            datasources.append({
                "name": name, "type": kind, "endpoint": _endpoint(cfg),
                "detail": _public_detail(cfg), "rules": used_by.get(name, []),
            })
    # Sinks configured only inline in bundle.json rules.
    for rule_tenant, rule in rules:
        name = rule.get("datasource", "")
        if name in seen:
            continue
        seen.add(name)
        inline = {k: v for k, v in rule.items() if k not in views.rule_base(rule, rule_tenant) and not k.startswith("_")}
        datasources.append({
            "name": name, "type": views.sink_type(rule), "endpoint": _endpoint({
                "endpoint": inline.get("mongo_uri") or inline.get("s3_endpoint_url"),
                "hosts": inline.get("zk_hosts") or inline.get("redis_hosts"),
                "host": inline.get("ssh_host"), "port": inline.get("ssh_port"),
                "namespace": inline.get("k8s_namespace") or inline.get("helm_namespace"),
            }),
            "detail": _public_detail({k: v for k, v in inline.items() if k not in (
                "repo_url", "file_name", "repositories", "branch", "branch_map", "tenant_id", "branch_as_env",
                "pr_as_env", "tag_as_env", "self_heal", "prune", "prune_last", "sync_mode", "destination_template",
                "root_key", "platform", "datasource", "force", "tenants", "suspend", "drift_ignore")}),
            "rules": used_by.get(name, []),
        })

    secret_managers = []
    for wrapper in bundle.get("secret_managers", []) or bundle.get("secretManagers", []) or []:
        for kind, cfg in wrapper.items():
            secret_managers.append({"name": cfg.get("tag") or kind, "type": kind, "endpoint": cfg.get("endpoint", "")})

    schema = bundle.get("schema_registry") or bundle.get("schemaRegistry")
    view: dict[str, Any] = {
        "tenants": sorted({t for t, _ in rules}),
        "platforms": platforms,
        "datasources": datasources,
        "secretManagers": secret_managers,
    }
    if schema:
        view["schemaRegistry"] = {
            "platform": schema.get("platform", ""), "repo": schema.get("repo", ""),
            "branch": schema.get("branch", "main"), "files": [],
        }
    return view
