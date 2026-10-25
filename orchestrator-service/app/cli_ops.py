"""
app/cli_ops.py
──────────────
What the CLI API does — sync a file, validate a file, report on sync tasks.
The gRPC CLIService (app.grpc_server.cli_servicer) serves it to the gateway,
which runs the OIDC, RBAC and OPA checks first.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FORMATS = {
    "yaml": "yaml", "yml": "yaml",
    "json": "json",
    "toml": "toml",
    "env":  "env",
    "ini":  "ini",
    "hcl":  "hcl",
    "properties": "properties",
}

# Celery states → the TaskState names the CLI shows.
_CELERY_STATES = {
    "PENDING":  "PENDING",
    "RECEIVED": "PENDING",
    "STARTED":  "RUNNING",
    "PROGRESS": "RUNNING",
    "RETRY":    "RETRYING",
    "FAILURE":  "FAILED",
    "REVOKED":  "FAILED",
    "SUCCESS":  "SUCCEEDED",
}


def detect_format(file_path: str, hint: Optional[str]) -> str:
    if hint:
        return hint.lower()
    return _FORMATS.get(Path(file_path).suffix.lower().lstrip("."), "yaml")


def resolve_tenant(*candidates: Optional[str]) -> str:
    for c in candidates:
        if c and c.strip():
            return c.strip()
    return "default"


def enqueue_sync(
    *,
    datasource: str,
    env: str,
    file_path: str,
    content: str,
    fmt: str,
    tenant_id: str,
    dry_run: bool,
    label: str,
    submitted_by: str,
    revision: str = "",
    platform: str = "",
) -> str:
    """Queue process_cli_sync_task and return its task id."""
    from app.tasks.etl import process_cli_sync_task

    logger.info(
        "cli sync  tenant=%s  datasource=%s  env=%s  file=%s  revision=%s  dry_run=%s  user=%s",
        tenant_id, datasource, env, file_path, revision, dry_run, submitted_by,
    )
    task = process_cli_sync_task.delay(
        datasource=datasource,
        env=env,
        file_path=file_path,
        content=content,
        fmt=fmt,
        tenant_id=tenant_id,
        dry_run=dry_run,
        label=label,
        submitted_by=submitted_by,
        revision=revision,
        platform=platform,
    )
    return task.id


# A branch or tag name as git takes it: never an option ("-…"), no "..".
_REVISION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")


def valid_revision(revision: str) -> bool:
    return bool(_REVISION.fullmatch(revision)) and ".." not in revision \
        and not revision.endswith((".lock", "/", "."))


def file_at_revision(rule: dict[str, Any], env: str, revision: str) -> tuple[str, str]:
    """The rule's file for *env* as it is in Git at the branch or tag
    *revision*: (path, content).
    Raises ValueError when there is no such file."""
    from app.pipeline.git_extractor import GitExtractor

    repo_url = rule.get("repo_url")
    if not repo_url:
        raise ValueError(f"--revision needs a rule that names one repository: the {rule.get('datasource')} "
                         f"rule names {', '.join(rule.get('repositories') or []) or 'none'}")
    path = (rule.get("file_path_map") or {}).get(env) or rule.get("file_name")
    if not path:
        raise ValueError(f"the {rule.get('datasource')} rule names no file for {env}")
    content = GitExtractor().extract_file(repo_url, revision, path, token=rule.get("token"))
    if content is None:
        raise ValueError(f"{path} is not in {repo_url} at {revision}")
    return path, content


def validate_content(
    *,
    file_path: str,
    content: str,
    fmt: str,
    datasource: Optional[str],
    tenant_id: str,
    platform: str = "",
) -> tuple[str, list[str], int]:
    """Check content the way a sync of it would be checked, without writing.

    Parses it (as fmt), takes the root_key subtree and variables_map of the
    datasource's rule when a datasource is named, flattens it, and validates
    the keys against the tenant's CUE schema for the file.

    Returns (status, messages, key_count); status is ok, warn (nothing could
    be checked) or failed.
    """
    import json

    from app.parsers.loader import load_as
    from app.pipeline import transformer
    from app.pipeline.schema_utils import AmbiguousRule, rule_config_for_datasource
    from app.pipeline.stage_etl import _DEFAULT_ROOT_KEY
    from app.pipeline.validator import CUEValidationError, Validator
    from app.schema_registry.manager import get_schema_manager

    try:
        parsed = load_as(content, file_path, fmt)
    except Exception as exc:
        return "failed", [f"parse error: {exc}"], 0

    rule: dict[str, Any] = {}
    messages: list[str] = []
    if datasource:
        try:
            rule = rule_config_for_datasource(tenant_id, datasource, platform) or {}
        except AmbiguousRule as exc:
            return "failed", [str(exc)], 0
        if not rule:
            messages.append(f"no rule for datasource {datasource!r} in tenant {tenant_id!r}; checked the whole file")

    root_key = rule.get("root_key", _DEFAULT_ROOT_KEY)
    if root_key and isinstance(parsed, dict):
        if root_key in parsed:
            parsed = parsed[root_key]
        elif root_key != _DEFAULT_ROOT_KEY:
            return "failed", [f"root_key {root_key!r} not found in {file_path}"], 0

    structured = isinstance(parsed, (dict, list))
    flat = transformer.transform(
        content=json.dumps(parsed) if structured else content,
        file_path=file_path,
        variables_map=rule.get("variables_map", {}),
        env="",
        root_key="",
        parsed=parsed if structured else None,
    )

    name = Path(file_path).name
    schema_dir = get_schema_manager().tenant_schema_root(tenant_id)
    validator = Validator(schema_dir=schema_dir if schema_dir and schema_dir.exists() else None)
    if validator._find_schema(name) is None:
        return "warn", messages + [f"no schema for {name} in tenant {tenant_id!r}; nothing was checked"], len(flat)
    if not validator._is_cue_available():
        return "warn", messages + ["the cue binary is not installed on the orchestrator; nothing was checked"], len(flat)
    try:
        validator.validate(flat_data=flat, file_name=name, strict=True)
    except CUEValidationError as exc:
        return "failed", messages + [str(exc)], len(flat)
    return "ok", messages, len(flat)


def _ledger_task(task_id: str) -> Optional[dict[str, Any]]:
    try:
        from app.ui import ledger
        return ledger.get_task(task_id)
    except Exception:
        logger.debug("cli task %s: ledger unavailable", task_id, exc_info=True)
        return None


def _view(task_id: str, record: Optional[dict[str, Any]], result: Any, celery_state: str) -> dict[str, Any]:
    """One task as the CLI shows it, from its ledger record and Celery result."""
    record = record or {}
    result = result if isinstance(result, dict) else {}

    state = _CELERY_STATES.get(celery_state, "PENDING")
    if celery_state == "SUCCESS" and result.get("state"):
        state = result["state"]          # ok() / fail() say how the sync went
    elif celery_state == "PENDING" and record.get("state"):
        state = record["state"]          # the ledger knows tasks Celery forgot

    files = result.get("files") or record.get("files") or []
    message = result.get("message") or _dry_run_summary(result) or record.get("message") or ""
    revision = record.get("revision") or ""
    return {
        "task_id":     task_id,
        "tenant_id":   result.get("tenant_id") or "",
        "state":       state,
        "message":     message,
        "datasource":  result.get("datasource") or record.get("datasource") or "",
        "env":         result.get("env") or record.get("env") or "",
        "file_path":   next((f.get("file", "") for f in files if isinstance(f, dict)), ""),
        "dry_run":     bool(result.get("dry_run", record.get("dryRun", False))),
        "label":       "" if revision == "cli-push" else revision,
        "written":     int(result.get("total_written") or 0),
        "pruned":      int(result.get("total_pruned") or 0),
        "platform":    record.get("_platform") or str(record.get("rule") or "").split("/", 1)[0],
        "created_at":  record.get("createdAt") or "",
        "finished_at": record.get("finishedAt") or "",
        "error":       message if state == "FAILED" else "",
    }


def _dry_run_summary(result: dict[str, Any]) -> str:
    """What a dry run would have written, file by file — keys and validation."""
    if not result.get("dry_run"):
        return ""
    parts = [
        f"{f.get('file', '?')}: {f.get('keys', 0)} keys, validation {f.get('validation', 'ok')}"
        for f in result.get("files") or [] if isinstance(f, dict)
    ]
    return "dry run — would write " + "; ".join(parts) if parts else "dry run — nothing to write"


def task_view(task_id: str) -> dict[str, Any]:
    """The state of a CLI sync task. An id Celery does not know reads PENDING,
    as it does for a task queued but not yet picked up."""
    from celery.result import AsyncResult
    from app.worker.celery import celery

    r = AsyncResult(task_id, app=celery)
    try:
        result = r.result
    except Exception:
        result = None
    return _view(task_id, _ledger_task(task_id), result, r.state)


def list_tasks(tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent CLI-triggered tasks for a tenant, newest first, from the ledger."""
    from app.ui import ledger

    tasks = []
    for record in ledger.list_tasks(tenant_id, limit=ledger.MAX_TASKS):
        if record.get("trigger") != "cli":
            continue
        tasks.append(_view(record.get("taskId", ""), record, None, "PENDING"))
        if len(tasks) >= limit:
            break
    return tasks


def rule_file_for(rule: dict[str, Any], file_path: str) -> str:
    """The file of the rule that a pushed file stands for.

    A CLI push names a local path (./configs/app-settings.yaml); the rule
    tracks the file by its path in the repo (app-settings.yaml). Sinks key
    what they store by that path, so a push matched to it replaces exactly
    what a Git push of the file writes, instead of landing next to it. A
    file that matches none of the rule's files keeps its own path.
    """
    pushed = file_path.replace("\\", "/")
    name = pushed.rsplit("/", 1)[-1]
    tracked = [rule.get("file_name")] + list((rule.get("file_path_map") or {}).values())
    for path in tracked:
        if isinstance(path, str) and path and (path == pushed or path.rsplit("/", 1)[-1] == name):
            return path
    return file_path
