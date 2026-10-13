"""
app/ui/views.py
────────────────
Builds the JSON the web UI reads: rules from the tenant's bundle, one target
per environment that has been synced, and — for each target — the current
state read back from the sink compared with the desired state from Git.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.ui import ledger

logger = logging.getLogger(__name__)

SINK_TYPES = ("mongo", "redis", "zookeeper", "s3", "configmap", "helm", "vercel", "linux_server")

# Rule fields that only tell how to connect to a sink.
_SINK_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("mongo", ("mongo_uri", "database", "collection")),
    ("zookeeper", ("zk_hosts",)),
    ("redis", ("redis_hosts", "redis_host")),
    ("s3", ("s3_bucket",)),
    ("linux_server", ("ssh_host", "file_path")),
    ("configmap", ("k8s_namespace", "k8s_kubeconfig_path", "configmap_name")),
    ("helm", ("helm_release_name", "helm_chart", "helm_namespace")),
    ("vercel", ("vercel_project_id", "vercel_token")),
]

# A comparison is answered from the cache for this long; after that the cached
# one is still answered at once while the sink is read again in the background
# (stale-while-revalidate), so a page never waits on a sink it has read before.
COMPARE_TTL_SECONDS = float(os.getenv("UI_COMPARE_TTL_SECONDS", "10"))
# (tenant, datasource, env) → (read started at, desired-state task id, result)
_compare_cache: dict[tuple, tuple[float, str | None, dict]] = {}
_compare_reading: dict[tuple, threading.Event] = {}  # reads in progress, by cache key
_compare_lock = threading.Lock()
_refresh_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ui_refresh")


def sink_type(rule: dict) -> str:
    ds = str(rule.get("datasource", "")).lower().replace("-", "_")
    if ds in SINK_TYPES:
        return ds
    for kind in SINK_TYPES:
        if ds.startswith(kind + "_") or ds.startswith(kind):
            return kind
    for kind, hints in _SINK_HINTS:
        if any(h in rule for h in hints):
            return kind
    return "mongo"


def rule_name(rule: dict) -> str:
    return f"{rule.get('platform', 'github')}/{rule.get('datasource', '')}"


def _sync_mode(raw: Any) -> str:
    s = str(raw or "AUTO").upper()
    s = s[len("SYNC_MODE_"):] if s.startswith("SYNC_MODE_") else s
    return "GIT_UPSERT_ALL" if s == "FULL" else s


def _prune(rule: dict) -> dict:
    p = rule.get("prune", False)
    if isinstance(p, dict):
        enabled, last, dry = True, bool(p.get("last", False)), bool(p.get("dry_run", False))
    else:
        enabled, last, dry = bool(p), bool(rule.get("prune_last", False)), bool(rule.get("dry_run_prune", False))
    return {"enabled": enabled, "last": last, "dryRun": dry, "protection": list(rule.get("prune_protection", []) or [])}


def destination(rule: dict, tenant: str, env: str, *, placeholders: bool = False) -> str:
    """Where this rule writes for *env*, as the sink would resolve it."""
    kind = sink_type(rule)
    if kind == "linux_server" and not placeholders:
        from app.pipeline.sinks.linux_server import _safe_filename
        env = _safe_filename(env)  # the sink names files after the env this way
    tpl = rule.get("destination_template") or ""
    if tpl:
        try:
            return tpl.format(tenant=tenant, env=env)
        except (KeyError, IndexError, ValueError):
            return tpl
    if kind == "linux_server":
        # LinuxServerSink._resolve_path
        from pathlib import PurePosixPath
        base = PurePosixPath(rule.get("file_path", "/etc/vartrack/config.env"))
        if rule.get("env_as_dir", False):
            return str(base.parent / env / base.name)
        if rule.get("env_as_filename", True):
            return str(base.parent / f"{base.stem}_{env}{base.suffix}")
        return str(base)
    if kind == "mongo":
        return f"{rule.get('database', 'vartrack')}.{rule.get('collection', 'variables')}"
    if kind == "zookeeper":
        return f"{rule.get('zk_base_path', '').rstrip('/')}/{tenant}/{env}"
    if kind == "redis":
        return rule.get("redis_key_prefix") or f"{tenant}:{rule.get('datasource')}:{env}"
    if kind == "s3":
        # S3Sink._key_prefix
        prefix = f"{str(rule.get('s3_key_prefix', '')).rstrip('/')}/{env}".lstrip("/")
        return f"s3://{rule.get('s3_bucket', '')}/{prefix}/"
    return rule.get("datasource", "")


def destination_template(rule: dict) -> str:
    if rule.get("destination_template"):
        return rule["destination_template"]
    kind = sink_type(rule)
    if kind == "mongo":
        return f"{rule.get('database', 'vartrack')}.{rule.get('collection', 'variables')}"
    return destination(rule, "{tenant}", "{env}", placeholders=True)


def rule_base(rule: dict, tenant: str) -> dict[str, Any]:
    repos = list(rule.get("repositories") or [])
    if not repos and rule.get("repo_url"):
        repos = [ledger.repo_name(rule["repo_url"])]
    file_path_map = rule.get("file_path_map") or {}
    return {
        "name": rule_name(rule),
        "tenant": tenant,
        "platform": rule.get("platform", "github"),
        "datasource": rule.get("datasource", ""),
        "sinkType": sink_type(rule),
        "repositories": repos,
        "excludeRepositories": list(rule.get("exclude_repositories") or []),
        **({"fileName": rule["file_name"]} if rule.get("file_name") else {}),
        # bundle.json maps env → path; the UI shows path → env.
        "filePathMap": {path: env for env, path in file_path_map.items()} if file_path_map else {},
        "branchMap": dict(rule.get("branch_map") or {}),
        "envAsBranch": bool(rule.get("env_as_branch", False)),
        "envAsPr": bool(rule.get("env_as_pr", False)),
        "envAsTags": bool(rule.get("env_as_tags", False)),
        "destinationTemplate": destination_template(rule),
        "syncMode": _sync_mode(rule.get("sync_mode")),
        "selfHeal": bool(rule.get("self_heal", False)),
        "dryRun": bool(rule.get("dry_run", False)),
        "prune": _prune(rule),
        **({"secretManager": rule["secret_manager"]} if rule.get("secret_manager") else {}),
    }


# ── Current vs desired ────────────────────────────────────────────────────────

def _read_current(rule: dict, tenant: str, env: str, keys: list[str]) -> tuple[dict[str, str] | None, str | None]:
    """Current values of *keys* in the sink, or (None, reason) when they can't be read."""
    from app.pipeline.sinks import create_sink
    from app.pipeline.sinks.base import BaseSink

    datasource = rule.get("datasource", "")
    try:
        sink = create_sink(datasource=datasource, rule_config=rule, tenant_id=tenant)
    except Exception as exc:
        return None, f"can't connect to {sink_type(rule)}: {exc}"
    try:
        if type(sink).read_values is BaseSink.read_values:
            return None, f"reading current values from {sink_type(rule)} isn't supported yet"
        return sink.read_values(keys, datasource, env), None
    except Exception as exc:
        return None, f"read failed: {exc}"
    finally:
        try:
            sink.close()
        except Exception:
            pass


def _cache_key(rule: dict, tenant: str, record: dict) -> tuple:
    return (tenant, rule.get("datasource"), record["env"])


def _usable(hit: tuple | None, record: dict) -> bool:
    # Only a comparison against the desired state this record holds: one made
    # before the last sync says nothing about what that sync wrote.
    return hit is not None and hit[1] == record.get("lastSync", {}).get("taskId")


def compare(rule: dict, tenant: str, record: dict, *, fresh: bool = False) -> dict[str, Any]:
    """
    Compare a target's desired state with the sink.

    Answered from the cache: as is within COMPARE_TTL_SECONDS, and after that
    still at once while a background read refreshes it. Only a target not read
    since its last sync waits for the sink. *fresh* always reads now.
    """
    key = _cache_key(rule, tenant, record)
    if fresh:
        # The watcher's drift check: a read that began earlier may predate the write it asks about.
        return _compare_now(rule, tenant, record, key)
    while True:
        with _compare_lock:
            hit = _compare_cache.get(key)
            if not _usable(hit, record):
                hit = None
            if hit and time.time() - hit[0] < COMPARE_TTL_SECONDS:
                return hit[2]
            reading = _compare_reading.get(key)
            if reading is None:
                reading = _compare_reading[key] = threading.Event()
                break
            if hit:
                return hit[2]  # stale, and already being read again
        # Another request is reading this sink for a comparison we lack: share its answer.
        reading.wait()
    if hit:
        try:
            _refresh_pool.submit(_read_and_release, rule, tenant, record, key, reading)
        except RuntimeError:  # pool shut down at interpreter exit
            _release(key, reading)
        return hit[2]
    return _read_and_release(rule, tenant, record, key, reading)


def _read_and_release(rule: dict, tenant: str, record: dict, key: tuple, reading: threading.Event) -> dict[str, Any]:
    try:
        return _compare_now(rule, tenant, record, key)
    finally:
        _release(key, reading)


def _release(key: tuple, reading: threading.Event) -> None:
    with _compare_lock:
        _compare_reading.pop(key, None)
    reading.set()


def _compare_now(rule: dict, tenant: str, record: dict, cache_key: tuple) -> dict[str, Any]:
    now = time.time()
    env = record["env"]
    desired: dict[str, str] = record.get("desired", {})
    digests: dict[str, str] = record.get("secretDigests", {})
    current, error = _read_current(rule, tenant, env, list(desired))
    keys: list[dict[str, Any]] = []
    changed = missing = 0
    if current is not None:
        protection = _prune(rule)["protection"]
        for key in sorted(desired):
            secret = key in digests
            live = current.get(key)
            if live is None:
                state = "missing"
                missing += 1
            elif (ledger.secret_digest(live) == digests[key]) if secret else (live == desired[key]):
                state = "synced"
            else:
                state = "drifted"
                changed += 1
            keys.append({
                "key": key,
                "gitValue": desired[key],
                **({"liveValue": ledger.SECRET_MASK if secret else live} if live is not None else {}),
                "secret": secret,
                "protected": any(fnmatch.fnmatch(key, p) for p in protection),
                "state": state,
                "sourceFile": (record.get("files") or [""])[0],
            })
    result = {"error": error, "keys": keys, "differs": changed + missing, "checkedAt": ledger.now_iso(now)}
    task_id = record.get("lastSync", {}).get("taskId")
    with _compare_lock:
        prev = _compare_cache.get(cache_key)
        # A slower read that began earlier must not replace a newer one.
        if prev is None or prev[1] != task_id or prev[0] <= now:
            _compare_cache[cache_key] = (now, task_id, result)
    return result


def compare_all(rules: list[dict], tenant: str, records: list[dict]) -> None:
    """
    Bring every target's comparison into the cache before the views are built.
    Targets already compared answer at once (refreshed behind if stale); the
    rest are read together, so a sink that is slow to answer (or unreachable)
    delays the list by its own wait, not by the sum of everyone's.
    """
    pairs = [(rule, rec) for rule in rules for rec in records if rec.get("datasource") == rule.get("datasource")]
    cold = []
    for rule, rec in pairs:
        with _compare_lock:
            hit = _compare_cache.get(_cache_key(rule, tenant, rec))
        if _usable(hit, rec):
            compare(rule, tenant, rec)
        else:
            cold.append((rule, rec))
    if not cold:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(cold)), thread_name_prefix="ui_compare") as pool:
        list(pool.map(lambda p: compare(p[0], tenant, p[1]), cold))


def _drift_marker(tenant: str, rule: dict, env: str, differs: bool) -> str | None:
    """Remember when drift was first seen, and emit DriftDetected once per episode."""
    try:
        r = ledger._redis()
        key = f"vt:ui:drift:{tenant}:{rule.get('datasource')}:{env}"
        if not differs:
            r.delete(key)
            return None
        since = ledger.now_iso()
        if r.set(key, since, nx=True):
            ledger.record_event(
                tenant=tenant, reason="DriftDetected", warning=True, rule=rule_name(rule), env=env,
                message=f"current state in {sink_type(rule)} differs from Git",
            )
            return since
        return r.get(key)
    except Exception:
        return None


def target_view(rule: dict, tenant: str, record: dict, *, recent_tasks: list[dict]) -> dict[str, Any]:
    env = record["env"]
    cmp = compare(rule, tenant, record)
    last_sync = record.get("lastSync") or None
    target: dict[str, Any] = {
        "env": env,
        "repository": record.get("repository", ""),
        "ref": record.get("ref", ""),
        "destination": destination(rule, tenant, env),
        "revision": record.get("revision") or None,
        "commitMessage": record.get("commitMessage") or None,
        "author": record.get("author") or None,
        "keyCount": len(record.get("desired", {})),
        "lastPolledAt": cmp["checkedAt"],
        **({"lastSync": last_sync} if last_sync else {}),
    }
    # A sync for this environment that is still running supersedes the last finished one.
    running = next((t for t in recent_tasks if t.get("env") == env and ledger.is_active(t)), None)
    if running:
        target["lastSync"] = {
            "taskId": running["taskId"], "state": running["state"], "trigger": running["trigger"],
            "startedAt": running["createdAt"], "written": 0, "pruned": 0, "message": running.get("message", ""),
        }

    if cmp["error"]:
        target.update(drift="unknown", driftMessage=cmp["error"])
        return target
    if not cmp["differs"]:
        _drift_marker(tenant, rule, env, False)
        target["drift"] = "in_sync"
        return target

    since = _drift_marker(tenant, rule, env, True)
    target.update(driftedKeys=cmp["differs"], driftDetectedAt=since)
    heal_running = running is not None and running.get("trigger") == "self_heal"
    last_heal = next((t for t in recent_tasks if t.get("env") == env and t.get("trigger") == "self_heal"), None)
    if heal_running:
        target["drift"] = "healing"
    elif last_heal and last_heal.get("state") == "FAILED" and since and last_heal.get("finishedAt", "") >= since:
        target.update(drift="heal_failed", driftMessage=last_heal.get("message"))
    else:
        target["drift"] = "drifted"
    return target


def rule_view(rule: dict, tenant: str, records: list[dict], tasks: list[dict]) -> dict[str, Any]:
    name = rule_name(rule)
    rule_tasks = [t for t in tasks if t.get("rule") == name]
    targets = [
        target_view(rule, tenant, rec, recent_tasks=rule_tasks)
        for rec in sorted(records, key=lambda r: r.get("env", ""))
        if rec.get("datasource") == rule.get("datasource")
    ]
    return {**rule_base(rule, tenant), "targets": targets}


def key_entries(rule: dict, tenant: str, record: dict) -> list[dict[str, Any]]:
    cmp = compare(rule, tenant, record)
    if cmp["error"]:
        raise RuntimeError(cmp["error"])
    return cmp["keys"]


def public_task(task: dict) -> dict:
    """Drop the recorder's private fields before sending a task to the UI."""
    out = {k: v for k, v in task.items() if not k.startswith("_") and k != "tenant"}
    if out.get("state") in ledger.ACTIVE_STATES and not ledger.is_active(task):
        out.update(state="FAILED", message="no result recorded — the worker stopped before the task finished")
    return out
