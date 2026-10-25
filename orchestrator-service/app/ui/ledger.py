"""
app/ui/ledger.py
─────────────────
What the web UI reads back: for every (tenant, datasource, env) the desired
state that was last written from Git, a summary of every task, and a short
event stream. Kept in Redis next to the Celery broker.

Every write here is best-effort — a ledger failure is logged and never
fails a sync.

Keys
────
  vt:ui:target:{tenant}:{datasource}:{env}   JSON  last desired state + last sync
                                             (desiredByFile is keyed "{repo}:{file}": the inventory
                                             prune diffs; pendingPrune holds keys still to delete)
  vt:ui:targets:{tenant}                     SET   "{datasource}|{env}"
  vt:ui:task:{task_id}                       JSON  task summary (7 days)
  vt:ui:tasks:{tenant}                       LIST  task ids, newest first
  vt:ui:events:{tenant}                      LIST  event JSON, newest first
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

TASK_TTL_SECONDS = 7 * 24 * 3600
MAX_TASKS = 500
MAX_EVENTS = 1000
SECRET_MASK = "***"

# Celery kills a task after task_time_limit (600 s). A task still recorded as
# running after this long died without recording its end (OOM kill, worker
# restart), and must not keep the watcher waiting for it.
TASK_STALE_AFTER_SECONDS = 15 * 60
ACTIVE_STATES = ("PENDING", "RUNNING", "RETRYING")

# Header carried by tasks that were not started by a Git push.
TRIGGER_HEADER = "x-vartrack-trigger"

_client = None


def _redis():
    global _client
    if _client is None:
        import redis
        from app.config import settings
        _client = redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True, socket_timeout=3)
    return _client


def _key(*parts: str) -> str:
    return "vt:ui:" + ":".join(parts)


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def secret_digest(value: str) -> str:
    """Short digest so secret values can be compared without being stored."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def repo_name(repo_url: str) -> str:
    """'http://gitea:3000/acme/configs.git' → 'acme/configs'."""
    url = (repo_url or "").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        url = url.split("://", 1)[1]
        url = url.split("/", 1)[1] if "/" in url else url
    if "@" in url and ":" in url.split("@", 1)[1]:  # git@host:owner/repo
        url = url.split(":", 1)[1]
    return url


def inventory_key(repo_url: str, file_path: str) -> str:
    """The key of one file in a target's desiredByFile: its repository and
    path, since two repositories may both feed an environment a file of the
    same name."""
    return f"{repo_name(repo_url)}:{file_path}"


def key_state(desired_value: str, live: Any, digest: str | None = None) -> str:
    """'unreadable' | 'missing' | 'synced' | 'drifted' — one key, as the sink
    holds it (*live*) against what varTrack wrote. A secret is compared by
    *digest*, since its value is not stored. The drift view and prune both
    decide a key's state here."""
    from app.pipeline.sinks.base import Unreadable

    if isinstance(live, Unreadable):
        return "unreadable"  # write-only in the sink: not comparable
    if live is None:
        return "missing"
    same = secret_digest(live) == digest if digest is not None else live == desired_value
    return "synced" if same else "drifted"


def entries_of(target: dict[str, Any] | None) -> dict[str, dict]:
    """A target's desiredByFile entries; a target recorded before per-file
    tracking is one entry."""
    if not target:
        return {}
    by_file = target.get("desiredByFile")
    if by_file is not None:
        return by_file
    if target.get("desired"):
        return {",".join(target.get("files") or []): {
            "desired": target["desired"], "secretDigests": target.get("secretDigests", {})}}
    return {}


def is_active(task: dict[str, Any], now: float | None = None) -> bool:
    """Still running — not finished, and not abandoned by a worker that died."""
    if task.get("state") not in ACTIVE_STATES:
        return False
    try:
        started = datetime.fromisoformat(str(task["createdAt"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return True
    return (now if now is not None else time.time()) - started < TASK_STALE_AFTER_SECONDS


def trigger_from_headers(headers: dict[str, str] | None, default: str = "webhook") -> str:
    for k, v in (headers or {}).items():
        if k.lower() == TRIGGER_HEADER and v in ("webhook", "cli", "self_heal", "ui"):
            return v
    return default


# ── Writes ────────────────────────────────────────────────────────────────────

def record_target(
    *,
    tenant: str,
    platform: str,
    datasource: str,
    env: str,
    repo_url: str,
    ref: str,
    revision: str,
    file_path: str,
    flat_data: dict[str, Any],
    secret_fields: Iterable[str],
    commit_message: str,
    author: str,
    sync: dict[str, Any],
    pending_prune: Iterable[str] | None = None,
    key_space: str | None = None,
) -> None:
    """Remember the desired state just written for one environment.

    *key_space* is where the sync put its keys (stage_sync.key_space_id; empty
    for a sink that does not delete single keys). It is stored as keySpace so
    a later prune of another target can tell whose keys share its key space;
    None keeps what the target already holds, a target recorded before this
    has no keySpace at all.

    *pending_prune* is the keys the sync wanted to delete and could not (the
    next sync retries them); an empty list clears them, None keeps what the
    target already holds. Each is stored with the value (or secret digest)
    last written, so a retry can tell whether something else changed it."""
    try:
        r = _redis()
        key = _key("target", tenant, datasource, env)
        secrets = set(secret_fields)
        desired = {k: (SECRET_MASK if k in secrets else str(v)) for k, v in flat_data.items()}
        digests = {k: secret_digest(str(v)) for k, v in flat_data.items() if k in secrets}

        # Desired state is kept per file: a push syncs only the files it
        # touched, and the environment's other files still say what it holds.
        prev_raw = r.get(key)
        prev = json.loads(prev_raw) if prev_raw else {}
        by_file = dict(entries_of(prev))
        entry_key = inventory_key(repo_url, file_path)
        # Entries recorded before the key carried the repository: this file's.
        by_file.pop(file_path, None)
        by_file.pop(entry_key, None)
        by_file[entry_key] = {"file": file_path, "desired": desired, "secretDigests": digests}  # last written last

        # A key in two files holds whatever was written last.
        desired, digests = {}, {}
        for entry in by_file.values():
            for k, v in entry["desired"].items():
                desired[k] = v
                if k in entry["secretDigests"]:
                    digests[k] = entry["secretDigests"][k]
                else:
                    digests.pop(k, None)
        names: set[str] = set()
        for name, entry in by_file.items():
            if entry.get("file"):
                names.add(entry["file"])
            else:  # recorded before entries named their file: the key is the path(s)
                names.update(n for n in name.split(",") if n)
        files = sorted(names)
        prev_pending: dict[str, dict] = prev.get("pendingPrune") or {}
        wanted = prev_pending if pending_prune is None else pending_prune
        pending = {}
        for k in sorted(set(wanted)):
            if k in desired:
                continue  # written again: no longer stale
            if k in prev_pending:
                pending[k] = prev_pending[k]
            elif k in (prev.get("desired") or {}):
                pending[k] = {"value": prev["desired"][k], "digest": (prev.get("secretDigests") or {}).get(k)}

        doc = {
            "tenant": tenant,
            "platform": platform,
            "datasource": datasource,
            "env": env,
            "repository": repo_name(repo_url),
            "repoUrl": repo_url,
            "ref": ref,
            "revision": revision,
            "commitMessage": commit_message,
            "author": author,
            "files": files,
            "desired": desired,
            "secretDigests": digests,
            "desiredByFile": by_file,
            "pendingPrune": pending,
            "lastSync": sync,
        }
        space = key_space if key_space is not None else prev.get("keySpace")
        if space is not None:
            doc["keySpace"] = space
        r.set(key, json.dumps(doc))
        r.sadd(_key("targets", tenant), f"{datasource}|{env}")
    except Exception:
        logger.warning("ui ledger: record_target failed", exc_info=True)


def update_target_sync(*, tenant: str, datasource: str, env: str, sync: dict[str, Any]) -> None:
    """Attach a sync result (e.g. a failure) to an environment without changing its desired state."""
    try:
        r = _redis()
        key = _key("target", tenant, datasource, env)
        raw = r.get(key)
        if not raw:
            return
        doc = json.loads(raw)
        doc["lastSync"] = sync
        r.set(key, json.dumps(doc))
    except Exception:
        logger.warning("ui ledger: update_target_sync failed", exc_info=True)


def record_task(task: dict[str, Any], *, tenant: str, new: bool = False) -> None:
    """Store a task summary in the shape the UI reads (camelCase)."""
    try:
        r = _redis()
        r.set(_key("task", task["taskId"]), json.dumps({**task, "tenant": tenant}), ex=TASK_TTL_SECONDS)
        if new:
            lst = _key("tasks", tenant)
            # A retried task starts again under the same id: list it once.
            r.lrem(lst, 0, task["taskId"])
            r.lpush(lst, task["taskId"])
            r.ltrim(lst, 0, MAX_TASKS - 1)
    except Exception:
        logger.warning("ui ledger: record_task failed", exc_info=True)


def record_event(
    *,
    tenant: str,
    reason: str,
    message: str,
    warning: bool = False,
    rule: str | None = None,
    env: str | None = None,
    task_id: str | None = None,
) -> None:
    try:
        r = _redis()
        event = {
            "id": uuid.uuid4().hex,
            "time": now_iso(),
            "type": "Warning" if warning else "Normal",
            "reason": reason,
            "message": message,
            **({"rule": rule} if rule else {}),
            **({"env": env} if env else {}),
            **({"taskId": task_id} if task_id else {}),
        }
        lst = _key("events", tenant)
        r.lpush(lst, json.dumps(event))
        r.ltrim(lst, 0, MAX_EVENTS - 1)
    except Exception:
        logger.warning("ui ledger: record_event failed", exc_info=True)


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_targets(tenant: str) -> list[dict[str, Any]]:
    r = _redis()
    members = sorted(r.smembers(_key("targets", tenant)))
    if not members:
        return []
    keys = [_key("target", tenant, *m.split("|", 1)) for m in members]
    return [json.loads(raw) for raw in r.mget(keys) if raw]


def list_envs(tenant: str, datasource: str) -> list[str]:
    """The environments a datasource has been synced to."""
    members = _redis().smembers(_key("targets", tenant))
    return sorted(m.split("|", 1)[1] for m in members if m.split("|", 1)[0] == datasource)


def get_target(tenant: str, datasource: str, env: str) -> dict[str, Any] | None:
    raw = _redis().get(_key("target", tenant, datasource, env))
    return json.loads(raw) if raw else None


def get_task(task_id: str) -> dict[str, Any] | None:
    raw = _redis().get(_key("task", task_id))
    return json.loads(raw) if raw else None


def list_tasks(tenant: str, *, rule: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    r = _redis()
    ids = r.lrange(_key("tasks", tenant), 0, MAX_TASKS - 1)
    tasks: list[dict[str, Any]] = []
    for raw in r.mget([_key("task", i) for i in ids]) if ids else []:
        if not raw:
            continue
        task = json.loads(raw)
        if rule and task.get("rule") != rule:
            continue
        tasks.append(task)
        if len(tasks) >= limit:
            break
    return tasks


def list_events(tenant: str, *, rule: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    events = [json.loads(raw) for raw in _redis().lrange(_key("events", tenant), 0, MAX_EVENTS - 1)]
    if rule:
        events = [e for e in events if e.get("rule") == rule]
    return events[:limit]
