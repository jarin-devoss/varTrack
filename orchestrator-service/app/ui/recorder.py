"""
app/ui/recorder.py
───────────────────
Called by the Celery tasks at the start and end of every sync so the web UI
can show what happened. Every function is best-effort: it logs and returns
on any error, so recording can never fail a sync.
"""
from __future__ import annotations

import logging
from typing import Any

from app.ui import ledger

logger = logging.getLogger(__name__)


def _commit_info(parsed_payload: dict | None) -> tuple[str, str]:
    """Commit message and author from a push payload, when it has them."""
    p = parsed_payload or {}
    head = p.get("head_commit") or {}
    message = str(head.get("message") or "").split("\n", 1)[0]
    author = (head.get("author") or {}).get("name") or (p.get("pusher") or {}).get("name") or ""
    return message, str(author)


def task_started(
    task_id: str,
    *,
    tenant: str,
    platform: str,
    datasource: str,
    trigger: str,
    repo_url: str,
    ref: str,
    revision: str,
    dry_run: bool,
    parsed_payload: dict | None = None,
    env: str | None = None,
    submitted_by: str | None = None,
) -> dict[str, Any] | None:
    try:
        rule = f"{platform}/{datasource}"
        repo = ledger.repo_name(repo_url)
        message, author = _commit_info(parsed_payload)
        task = {
            "taskId": task_id,
            "state": "RUNNING",
            "message": "running",
            "trigger": trigger,
            "rule": rule,
            "datasource": datasource,
            **({"env": env} if env else {}),
            "repository": repo,
            "ref": ref,
            "revision": revision,
            "submittedBy": submitted_by or author or None,
            "dryRun": dry_run,
            "createdAt": ledger.now_iso(),
            "files": [],
            "_commitMessage": message,
            "_author": author,
            "_repoUrl": repo_url,
            "_platform": platform,
        }
        ledger.record_task(task, tenant=tenant, new=True)
        if trigger == "webhook":
            ledger.record_event(
                tenant=tenant, reason="WebhookReceived", rule=rule, task_id=task_id,
                message=f"push to {repo}@{ref}" + (f" ({message})" if message else ""),
            )
        ledger.record_event(
            tenant=tenant, reason="SyncStarted", rule=rule, env=env, task_id=task_id,
            message=f"{'dry run of' if dry_run else 'syncing'} {repo}@{ref} to {datasource}",
        )
        return task
    except Exception:
        logger.warning("ui recorder: task_started failed", exc_info=True)
        return None


def _file_result(r: Any, dry_run_file: dict | None = None) -> dict[str, Any]:
    if dry_run_file is not None:
        return {
            "file": dry_run_file.get("file", ""),
            "env": dry_run_file.get("env", ""),
            "status": "ok",
            "syncMode": "dry_run",
            "written": 0,
            "pruned": 0,
            "keys": dry_run_file.get("keys"),
            "validation": dry_run_file.get("validation"),
        }
    status = r.status
    if status == "error" and r.error and "validation" in r.error.lower():
        status = "validation_failed"
    return {
        "file": r.file_path,
        "env": r.env,
        "status": status,
        "syncMode": r.sync_mode or "",
        "written": r.written,
        "pruned": r.pruned,
        **({"rootKey": r.root_key} if r.root_key else {}),
        **({"error": r.error} if r.error else {}),
    }


def task_finished(
    task: dict[str, Any] | None,
    *,
    tenant: str,
    etl: Any = None,
    results: list[Any] | None = None,
    dry_run_files: list[dict] | None = None,
    message: str = "",
) -> None:
    """Record the outcome of a task and, for written files, the new desired state."""
    if not task:
        return
    try:
        results = results or []
        files = [_file_result(r) for r in results] if not dry_run_files else [_file_result(None, f) for f in dry_run_files]
        failed = [f for f in files if f["status"] not in ("ok", "empty", "skipped")]
        envs = sorted({f["env"] for f in files if f["env"] and f["env"] != "unknown"})
        written = sum(f["written"] for f in files)
        pruned = sum(f["pruned"] for f in files)

        task.update({
            "state": "FAILED" if failed else "SUCCEEDED",
            "finishedAt": ledger.now_iso(),
            "files": files,
            "message": message or (
                f"{len(failed)} of {len(files)} file(s) failed" if failed
                else f"{written} key{'s' if written != 1 else ''} written" + (f", {pruned} pruned" if pruned else "")
            ),
        })
        if len(envs) == 1:
            task["env"] = envs[0]

        if task.get("dryRun"):
            ledger.record_task(task, tenant=tenant)
            ledger.record_event(tenant=tenant, reason="SyncSucceeded", rule=task["rule"], task_id=task["taskId"],
                                message=f"dry run: {sum((f.get('keys') or 0) for f in files)} keys would be written, nothing changed")
            return

        # The task is marked finished only after the desired state it wrote is
        # recorded: in between, the watcher's CheckDatasource would see no sync
        # running, compare the new values with the old desired state, and heal.
        try:
            _record_envs(task, tenant=tenant, envs=envs, files=files, etl=etl)
        finally:
            ledger.record_task(task, tenant=tenant)
    except Exception:
        logger.warning("ui recorder: task_finished failed", exc_info=True)


_DONE = ("ok", "empty", "skipped")


def _rolls_back(datasource: str) -> bool:
    """
    Whether this datasource's sink undoes earlier writes when a later file
    fails. stage_sync stops at the first file that fails and asks the sink to
    roll back every file it wrote before — in any environment; sinks without
    snapshot/delete support keep what they wrote.
    """
    try:
        from app.pipeline.sinks import SINK_REGISTRY
        from app.pipeline.sinks.base import BaseSink
    except Exception:
        return True  # unknown: claim no new desired state
    key = datasource.lower().replace("-", "_")
    cls = SINK_REGISTRY.get(key) or SINK_REGISTRY.get(key.split("_")[0])
    if cls is None:
        return False
    return hasattr(cls, "restore_file_data") or cls.delete_file_data is not BaseSink.delete_file_data


def _record_envs(task: dict[str, Any], *, tenant: str, envs: list[str], files: list[dict], etl: Any) -> None:
    rule, datasource, trigger = task["rule"], task["datasource"], task["trigger"]
    etl_files = {(f.file_path, f.env): f for f in (etl.files if etl else [])}
    first_failure = next((f for f in files if f["status"] not in _DONE), None)
    rolled_back = first_failure is not None and _rolls_back(datasource)
    for env in envs:
        env_files = [f for f in files if f["env"] == env]
        env_failed = [f for f in env_files if f["status"] not in _DONE]
        failed = bool(env_failed) or rolled_back
        error = None
        if env_failed:
            error = env_failed[0].get("error", env_failed[0]["status"])
        elif rolled_back:
            error = f"rolled back: {first_failure['file']} failed — {first_failure.get('error', first_failure['status'])}"
        sync = {
            "taskId": task["taskId"],
            "state": "FAILED" if failed else "SUCCEEDED",
            "trigger": trigger,
            "startedAt": task["createdAt"],
            "finishedAt": task["finishedAt"],
            "written": sum(f["written"] for f in env_files),
            "pruned": sum(f["pruned"] for f in env_files),
            "message": task["message"],
            **({"error": error} if failed else {}),
        }

        # Only what is still in the sink becomes desired state: nothing after a
        # rollback; without one, the files that were written before the failure.
        kept = [] if rolled_back else [f for f in env_files if f["status"] == "ok"]
        recorded = False
        for f in kept:
            src = etl_files.get((f["file"], env))
            if src is None:
                continue
            recorded = True
            ledger.record_target(
                tenant=tenant,
                platform=task["_platform"],
                datasource=datasource,
                env=env,
                repo_url=task["_repoUrl"],
                ref=task["ref"],
                revision=task["revision"],
                file_path=f["file"],
                flat_data=src.flat_data,
                secret_fields=src.secret_fields,
                commit_message=task.get("_commitMessage", ""),
                author=task.get("_author", ""),
                sync=sync,
            )

        if failed:
            if not recorded:
                ledger.update_target_sync(tenant=tenant, datasource=datasource, env=env, sync=sync)
            reason = "ValidationFailed" if any(f["status"] == "validation_failed" for f in env_failed) else "SyncFailed"
            if trigger == "self_heal":
                reason = "HealFailed"
            ledger.record_event(tenant=tenant, reason=reason, warning=True, rule=rule, env=env,
                                task_id=task["taskId"], message=str(error))
            continue

        ledger.record_event(
            tenant=tenant, reason="HealSucceeded" if trigger == "self_heal" else "SyncSucceeded",
            rule=rule, env=env, task_id=task["taskId"],
            message=f"wrote {sync['written']} key{'s' if sync['written'] != 1 else ''} to {datasource}",
        )
        if sync["pruned"]:
            ledger.record_event(tenant=tenant, reason="KeysPruned", rule=rule, env=env, task_id=task["taskId"],
                                message=f"pruned {sync['pruned']} key{'s' if sync['pruned'] != 1 else ''} no longer in Git")


def task_errored(task: dict[str, Any] | None, *, tenant: str, error: str) -> None:
    if not task:
        return
    try:
        task.update({"state": "FAILED", "finishedAt": ledger.now_iso(), "message": error})
        ledger.record_task(task, tenant=tenant)
        ledger.record_event(
            tenant=tenant, reason="HealFailed" if task["trigger"] == "self_heal" else "SyncFailed",
            warning=True, rule=task["rule"], env=task.get("env"), task_id=task["taskId"], message=error,
        )
    except Exception:
        logger.warning("ui recorder: task_errored failed", exc_info=True)
