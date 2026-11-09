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
    preview: bool = False,
) -> dict[str, Any] | None:
    """*preview*: a dry run started to learn what a sync would delete. It is recorded (its page answers) but not
    listed, and it leaves no events: nobody asked for a sync, so nothing in the history says one ran."""
    try:
        rule = f"{platform}/{datasource}"
        repo = ledger.repo_name(repo_url)
        message, author = _commit_info(parsed_payload)
        task: dict[str, Any] = {
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
            **({"preview": True} if preview else {}),
        }
        ledger.record_task(task, tenant=tenant, new=not preview)
        if preview:
            return task
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


def task_queued(
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
    env: str | None = None,
    preview: bool = False,
) -> None:
    """A sync accepted and handed to the workers: it is on the Tasks page, and its page answers, from now
    rather than from when a worker picks it up (which, with the workers busy or down, may be a while).
    The worker's own record (task_started) replaces this one."""
    try:
        ledger.record_task_if_absent({
            "taskId": task_id,
            "state": "PENDING",
            "message": "queued",
            "trigger": trigger,
            "rule": f"{platform}/{datasource}",
            "datasource": datasource,
            **({"env": env} if env else {}),
            "repository": ledger.repo_name(repo_url),
            "ref": ref,
            "revision": revision,
            "submittedBy": None,
            "dryRun": dry_run,
            "createdAt": ledger.now_iso(),
            "files": [],
            "_repoUrl": repo_url,
            "_platform": platform,
            **({"preview": True} if preview else {}),
        }, tenant=tenant, listed=not preview)
    except Exception:
        logger.warning("ui recorder: task_queued failed", exc_info=True)


def _file_result(r: Any, dry_run_file: dict | None = None) -> dict[str, Any]:
    if dry_run_file is not None:
        return {
            "file": dry_run_file.get("file", ""),
            "env": dry_run_file.get("env", ""),
            # Nothing is written either way; a file the schema rejects still fails the dry run.
            "status": "validation_failed" if dry_run_file.get("validation") == "failed" else "ok",
            "syncMode": "dry_run",
            "written": 0,
            "pruned": 0,
            "keys": dry_run_file.get("keys"),
            "validation": dry_run_file.get("validation"),
            "schema": dry_run_file.get("schema"),
            "schemaSource": dry_run_file.get("schema_source"),
            **({"schemaNote": dry_run_file["schema_note"]} if dry_run_file.get("schema_note") else {}),
            **({"validationError": dry_run_file["validation_error"]} if dry_run_file.get("validation_error") else {}),
            # What a sync would add or change, key by key (views.would_change); a dry run only.
            **({"wouldChange": dry_run_file["would_change"]} if dry_run_file.get("would_change") else {}),
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
        **({"replaced": r.replaced, "replacedBy": r.replaced_by} if r.replaced else {}),
        "keySpace": r.key_space,
        **({"sharedWith": r.shared_with} if r.shared_with is not None else {}),
        **({"error": r.error} if r.error else {}),
        # Keys only: prune's values stay in the ledger's target, not in a task.
        **({"prunePending": r.prune_pending} if r.prune_pending is not None else {}),
        **({"pruneSkipped": r.prune_skipped} if r.prune_skipped else {}),
        **({"pruneError": r.prune_error} if r.prune_error else {}),
        **({"validation": r.validation, "schema": r.schema, "schemaSource": r.schema_source}
           if getattr(r, "validation", "") else {}),
        **({"schemaNote": r.schema_note} if getattr(r, "schema_note", "") else {}),
    }


def task_finished(
    task: dict[str, Any] | None,
    *,
    tenant: str,
    etl: Any = None,
    results: list[Any] | None = None,
    dry_run_files: list[dict] | None = None,
    would_prune: dict[str, list[str]] | None = None,
    message: str = "",
) -> None:
    """Record the outcome of a task and, for written files, the new desired state. *would_prune* is what a dry run
    found a sync would delete, per environment (empty when nothing, or when the rule does not prune)."""
    if not task:
        return
    try:
        results = results or []
        files = [_file_result(r) for r in results] if not dry_run_files else [_file_result(None, f) for f in dry_run_files]
        failed = [f for f in files if f["status"] not in _DONE]
        envs = sorted({f["env"] for f in files if f["env"] and f["env"] != "unknown"})
        written = sum(f["written"] for f in files)
        pruned = sum(f["pruned"] for f in files)
        checked = sum((f.get("keys") or 0) for f in files)

        task.update({
            "state": "FAILED" if failed else "SUCCEEDED",
            "finishedAt": ledger.now_iso(),
            "files": files,
            "message": message or (
                f"{len(failed)} of {len(files)} file(s) failed" if failed
                else f"{checked} key{'s' if checked != 1 else ''} checked, nothing written" if task.get("dryRun")
                else f"{written} key{'s' if written != 1 else ''} written" + (f", {pruned} pruned" if pruned else "")
            ),
        })
        if len(envs) == 1:
            task["env"] = envs[0]

        if task.get("dryRun"):
            if would_prune is not None:
                task["wouldPrune"] = would_prune
            ledger.record_task(task, tenant=tenant)
            if task.get("preview"):
                return
            ledger.record_event(tenant=tenant, reason="SyncFailed" if failed else "SyncSucceeded", rule=task["rule"],
                                task_id=task["taskId"],
                                message=f"dry run: {task['message']}" if failed
                                else f"dry run: {checked} keys would be written, nothing changed")
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


# "partial" and "rolled_back" are what a failure elsewhere in the sync did to a file that was written; the
# failure itself is the file with another status.
_DONE = ("ok", "empty", "skipped", "partial", "rolled_back")


def _rolls_back(datasource: str) -> bool:
    """
    Whether this datasource's sink undoes earlier writes when a later file
    fails (BaseSink.can_restore). stage_sync stops at the first file that fails
    and asks such a sink to roll back every file it wrote before — in any
    environment; the others keep what they wrote.
    """
    try:
        from app.pipeline.sinks import SINK_REGISTRY
    except Exception:
        return True  # unknown: claim no new desired state
    key = datasource.lower().replace("-", "_")
    cls = SINK_REGISTRY.get(key) or SINK_REGISTRY.get(key.split("_")[0])
    if cls is None:
        return False
    return bool(getattr(cls, "can_restore", False))


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
        elif rolled_back and first_failure:
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
        kept = [] if rolled_back else [f for f in env_files if f["status"] in ("ok", "partial")]
        # The keys prune could not delete stay owned, so the next sync retries them.
        pending_prune = next((f["prunePending"] for f in env_files if f.get("prunePending") is not None), None)
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
                pending_prune=pending_prune,
                key_space=f.get("keySpace", ""),
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
        _shared_key_space(tenant, rule, env, task["taskId"], env_files, datasource)
        for f in env_files:
            if f.get("replaced"):
                ledger.record_event(tenant=tenant, reason="ShapeChanged", rule=rule, env=env,
                                    task_id=task["taskId"], message=_shape_changed(f))
        if sync["pruned"]:
            ledger.record_event(tenant=tenant, reason="KeysPruned", rule=rule, env=env, task_id=task["taskId"],
                                message=f"pruned {sync['pruned']} key{'s' if sync['pruned'] != 1 else ''} no longer in Git")
        # The writes stand either way: a prune that failed or held back is a warning, not a failed sync.
        for f in env_files:
            if f.get("pruneError"):
                ledger.record_event(tenant=tenant, reason="PruneFailed", warning=True, rule=rule, env=env,
                                    task_id=task["taskId"], message=f"prune failed, retried next sync: {f['pruneError']}")
            if f.get("pruneSkipped"):
                keys = f["pruneSkipped"]
                ledger.record_event(tenant=tenant, reason="PruneSkipped", warning=True, rule=rule, env=env,
                                    task_id=task["taskId"],
                                    message=f"{len(keys)} key{'s' if len(keys) != 1 else ''} no longer in Git "
                                            f"changed outside varTrack, not deleted: {', '.join(keys[:5])}"
                                            + (" …" if len(keys) > 5 else ""))


def _shared_key_space(tenant: str, rule: str, env: str, task_id: str, env_files: list[dict], datasource: str) -> None:
    """One SharedKeySpace warning when the environment's prune found other
    targets holding keys in the same key space; the next sync that finds none
    clears it, so a new episode warns again. Skipped when prune did not look."""
    found = next((f["sharedWith"] for f in env_files if f.get("sharedWith") is not None), None)
    if found is None:
        return
    try:
        r = ledger._redis()
        marker = f"vt:ui:shared-key-space:{tenant}:{datasource}:{env}"
        if not found:
            r.delete(marker)
        elif r.set(marker, ledger.now_iso(), nx=True):
            ledger.record_event(
                tenant=tenant, reason="SharedKeySpace", warning=True, rule=rule, env=env, task_id=task_id,
                message=f"{datasource}/{env} shares its key space with {', '.join(found)}: "
                        "their keys are never pruned from here",
            )
    except Exception:
        logger.warning("ui recorder: shared key space marker failed", exc_info=True)


def _shape_changed(f: dict[str, Any]) -> str:
    n, strategy = f["replaced"], f.get("replacedBy", "")
    if strategy == "STRATEGY_KEY_VALUE":
        what = f"replaced the document of {f['file']} with one document per key"
    else:
        what = f"replaced {n} per-key document{'s' if n != 1 else ''} of {f['file']} with one document"
    return f"{what} (update_strategy {strategy})" if strategy else what


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
