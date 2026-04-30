"""
app/tasks/etl.py
─────────────────
Three Celery tasks that drive the VarTrack ETL pipeline.

Flow for process_webhook_task:
  Stage 1 · PAYLOAD  – parse raw webhook, resolve rule
  Stage 2 · ETL      – extract files from git, transform, validate
  Stage 3 · SYNC     – write to MongoDB (skipped on dry_run)

Progress is reported between stages via update_state(state="PROGRESS", ...)
so callers polling GET /v1/tasks/{id} can see which stage is running, not
just a binary STARTED/SUCCESS state.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.worker.celery import celery
from app.tasks.payload import token_from_headers
from app.tasks.result import ok, fail
from app.pipeline.schema_utils import rules_from_bundle_all
from app.pipeline.stage_payload import run_stage_payload
from app.pipeline.stage_etl import run_stage_etl
from app.pipeline.stage_sync import run_stage_sync, sync_result_to_dict
from app.pipeline.models import PayloadContext, ETLResult

logger = logging.getLogger(__name__)


# ── Observability helpers ───────────────────────────────────────────────────────

def _bind_log_context(**ctx: Any) -> None:
    """Bind key/value pairs into the structlog context-var store."""
    try:
        import structlog
        structlog.contextvars.bind_contextvars(**ctx)
    except Exception:
        pass   # structlog not installed or not configured — stdlib logging still works


def _clear_log_context() -> None:
    """Clear structlog context-vars at task end (prevents cross-task leakage)."""
    try:
        import structlog
        structlog.contextvars.clear_contextvars()
    except Exception:
        pass


def _observe(stage: str, elapsed: float) -> None:
    """Record stage timing to the metrics backend; silently ignored if unavailable."""
    try:
        from app.monitoring import get_metrics
        m = get_metrics()
        if m:
            m.observe_etl_stage(stage, elapsed)
    except Exception:
        pass


# ── Dry-run report builder ─────────────────────────────────────────────────────
# Lives here (task layer) because it shapes task output, not pipeline logic.
# stage_etl.py is pure ETL; presentation of ETL results belongs to the caller.

def _build_dry_run_report(
    task_id:     str,
    ctx:         PayloadContext,
    etl:         ETLResult,
    received_at: float,
) -> dict[str, Any]:
    """Build a dry-run simulation report from Stage 2 output (no writes)."""
    files_report = []
    total_keys   = 0
    for f in etl.files:
        keys = list(f.flat_data.keys())
        total_keys += len(keys)
        files_report.append({
            "file":       f.file_path,
            "env":        f.env,
            "root_key":   f.root_key,
            "keys":       len(keys),
            "validation": f.validation,
            "sample":     dict(list(f.flat_data.items())[:5]),
        })
    for skipped in etl.skipped:
        files_report.append({"file": skipped, "status": "skipped"})

    return ok(task_id, "dry_run_complete", **{
        "dry_run":     True,
        "received_at": received_at,
        "platform":    ctx.platform,
        "branch":      ctx.branch,
        "commit_sha":  ctx.commit_sha,
        "summary": {
            "total_files": len(etl.files),
            "skipped":     len(etl.skipped),
            "total_keys":  total_keys,
        },
        "files": files_report,
    })


# ── Tasks ──────────────────────────────────────────────────────────────────────

@celery.task(
    bind=True,
    name="app.tasks.etl.process_webhook_task",
    max_retries=3,
    # Narrow autoretry to TRANSIENT errors only.
    # autoretry_for=(Exception,) would retry code bugs (AttributeError,
    # KeyError, etc.) — flooding the queue and hiding real problems.
    # Only network/IO failures are worth retrying automatically.
    autoretry_for=(ConnectionError, TimeoutError, OSError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    acks_late=True,
    track_started=True,
)
def process_webhook_task(
    self,
    *,
    platform:    str,
    datasource:  str,
    raw_payload: str,
    headers:     dict[str, str],
    received_at: float,
    rule_config: dict,
    tenant_id:   str = "default",
    dry_run:     bool = False,
) -> dict[str, Any]:
    """Payload → ETL → Sync pipeline in a single Celery task."""
    from celery.exceptions import SoftTimeLimitExceeded

    task_id = self.request.id

    # ── bind task context for all downstream log calls ────────────────────────
    _bind_log_context(
        task_id=task_id,
        platform=platform,
        datasource=datasource,
        dry_run=dry_run,
    )

    # ── open a webhook span for the full task ─────────────────────────────────
    from app.monitoring.tracer import start_webhook_span, start_etl_span
    span = start_webhook_span(task_id=task_id, platform=platform, datasource=datasource)

    logger.info(
        "task_start id=%s platform=%s datasource=%s dry_run=%s",
        task_id, platform, datasource, dry_run,
    )

    _t0 = time.perf_counter()

    try:
        # ── Stage 1 · PAYLOAD ─────────────────────────────────────────────────
        self.update_state(state="PROGRESS", meta={"stage": "payload", "progress": 0.05})
        ctx = run_stage_payload(
            raw_payload=raw_payload,
            platform=platform,
            rule_config=rule_config,
            datasource=datasource,
            tenant_id=tenant_id,
        )
        if ctx is None:
            return fail(task_id, f"no rule for platform={platform} datasource={datasource}")
        if not ctx.repo_url or not ctx.ref:
            return fail(task_id, "missing repo_url or ref in rule_config / payload")

        logger.info("stage=payload ref=%s branch=%s commit=%s", ctx.ref, ctx.branch, ctx.commit_sha)

        # ── Repo-level overrides (vartrack.json) ──────────────────────────────
        # If the repo contains a vartrack.json at its root, its settings
        # override the matching central rule_config keys.  Infrastructure and
        # schema-registry settings are never overridden.
        from app.pipeline.repo_overrides import apply_repo_overrides
        ctx = apply_repo_overrides(ctx)

        # Rule-level dry_run: a rule stored in the schema bundle with
        # dry_run=true acts as a permanent shadow rule — Stage 3 is never
        # reached, even for normal webhook deliveries (not via /dry-run).
        # The /dry-run endpoint sets dry_run=True on the task *and* injects
        # it into rule_config, so both paths converge here.
        dry_run = dry_run or bool(ctx.rule_config.get("dry_run", False))

        # ── Stage 2 · ETL ─────────────────────────────────────────────────────
        self.update_state(
            state="PROGRESS",
            meta={"stage": "etl", "progress": 0.20, "branch": ctx.branch, "commit": ctx.commit_sha},
        )
        _t_etl = time.perf_counter()
        etl_span = start_etl_span(task_id=task_id, stage="etl")
        try:
            etl = run_stage_etl(ctx=ctx, datasource=datasource, tenant_id=tenant_id)
        except Exception as _exc:
            etl_span.end(_exc)
            raise
        else:
            etl_span.end()

        _observe("etl", time.perf_counter() - _t_etl)

        if not etl.files and not etl.skipped:
            logger.info("stage=etl result=no_files")
            return ok(task_id, "no_files", **{"dry_run": dry_run, "files": []})

        logger.info(
            "stage=etl transformed=%d skipped=%d errors=%d",
            len(etl.files), len(etl.skipped), len(etl.errors),
        )

        # Dry-run exits after ETL — no writes to any datasource.
        if dry_run:
            return _build_dry_run_report(task_id, ctx, etl, received_at)

        # ── Stage 3 · SYNC ────────────────────────────────────────────────────
        self.update_state(
            state="PROGRESS",
            meta={"stage": "sync", "progress": 0.60, "files": len(etl.files)},
        )
        _t_sync = time.perf_counter()
        sync_span = start_etl_span(task_id=task_id, stage="sync")
        try:
            sync_results = run_stage_sync(
                ctx=ctx,
                etl=etl,
                datasource=datasource,
                tenant_id=tenant_id,
                total_sources=len(etl.files),
            )
        except Exception as _exc:
            sync_span.end(_exc)
            raise
        else:
            sync_span.end()

        _observe("sync", time.perf_counter() - _t_sync)

        failed  = [r for r in sync_results if r.status not in ("ok", "empty")]
        overall = "completed" if not failed else "completed_with_errors"

        logger.info(
            "stage=sync written=%d errors=%d duration_ms=%.0f",
            sum(r.written for r in sync_results),
            len(failed),
            (time.perf_counter() - _t0) * 1000,
        )

        return ok(task_id, overall, **{
            "files":  [sync_result_to_dict(r) for r in sync_results],
            "errors": len(failed),
        })

    except SoftTimeLimitExceeded:
        span.end()
        logger.warning("task=%s hit soft time limit — will retry", task_id)
        raise self.retry(countdown=30, max_retries=1)

    except Exception as exc:
        span.end(exc)
        logger.exception("task=%s unhandled error: %s", task_id, exc)
        raise

    finally:
        _clear_log_context()


@celery.task(
    bind=True,
    name="app.tasks.etl.refresh_schema_task",
    max_retries=3,
    autoretry_for=(ConnectionError, TimeoutError, OSError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    acks_late=True,
    track_started=True,
)
def refresh_schema_task(
    self,
    *,
    platform:    str,
    repo:        str,
    branch:      str,
    raw_payload: str,
    headers:     dict[str, str],
    tenant_id:   str = "default",
) -> dict[str, Any]:
    """Refresh schema registry when the schema repo receives a push."""
    task_id = self.request.id
    logger.info("schema refresh task=%s tenant=%s repo=%s", task_id, tenant_id, repo)

    from app.schema_registry.manager import get_schema_manager
    mgr        = get_schema_manager()
    token      = token_from_headers(headers)
    mgr.invalidate(tenant_id)
    local_path = mgr.get_or_clone(tenant_id, repo, branch, token=token)
    cue_files  = list(local_path.rglob("*.cue"))
    return ok(task_id, "schema_refreshed", **{
        "tenant_id":  tenant_id,
        "repo":       repo,
        "branch":     branch,
        "cue_files":  len(cue_files),
        "local_path": str(local_path),
    })


@celery.task(
    bind=True,
    name="app.tasks.etl.sync_all_task",
    max_retries=1,
    acks_late=True,
    track_started=True,
    ignore_result=True,
)
def sync_all_task(
    self,
    *,
    rules:     list[dict],
    tenant_id: str | None = None,
) -> None:
    """
    Periodic self-heal task dispatched by Celery beat every 5 minutes.
    Dispatches process_webhook_task for every rule with self_heal=true.

    Git SHA resolution is parallelised via ThreadPoolExecutor (max 8 workers)
    — 100 repos at 1s each finish in ~13s instead of ~100s.

    tenant_id=None means "all tenants": rules_from_bundle_all(None) aggregates
    rules from every registered tenant, each rule stamped with its tenant_id
    for correct dispatch.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    task_id = self.request.id
    logger.info("sync_all task=%s rules_hint=%d tenant=%s", task_id, len(rules), tenant_id or "all")

    if not rules:
        rules = rules_from_bundle_all(tenant_id)

    heal_rules = [r for r in rules if r.get("self_heal", False)]
    if not heal_rules:
        logger.info("sync_all task=%s: no self_heal rules, done", task_id)
        return

    from app.pipeline.git_extractor import GitExtractor
    extractor = GitExtractor()

    def _resolve_sha(rule: dict) -> tuple[dict, str | None]:
        """Resolve the HEAD SHA for a rule's repo/branch (runs in thread pool)."""
        repo_url = rule.get("repo_url", "")
        branch   = rule.get("branch", "main")
        token    = rule.get("token")
        try:
            sha = extractor.get_commit_sha(repo_url, branch, token=token)
            return rule, sha
        except Exception:
            logger.exception("sync_all sha_resolve error repo=%s", repo_url)
            return rule, None

    healed, errors = [], []

    # Parallelise git ls-remote calls (I/O-bound).
    # max_workers=8: enough parallelism without overwhelming the git host.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sync_sha") as pool:
        futures = {pool.submit(_resolve_sha, rule): rule for rule in heal_rules}

        for future in as_completed(futures):
            rule, sha = future.result()
            if sha is None:
                errors.append(rule.get("repo_url", "?"))
                continue

            repo_url = rule.get("repo_url", "")
            branch   = rule.get("branch", "main")
            try:
                # Use per-rule tenant_id when rules came from multi-tenant fan-out.
                dispatch_tenant = rule.get("tenant_id") or tenant_id or "default"
                process_webhook_task.apply_async(
                    kwargs={
                        "platform":    rule.get("platform", "github"),
                        "datasource":  rule.get("datasource", ""),
                        "raw_payload": json.dumps({
                            "ref":        f"refs/heads/{branch}",
                            "after":      sha,
                            "repository": {"full_name": repo_url},
                            "commits":    [],
                        }),
                        "headers":     {},
                        "received_at": time.time(),
                        "rule_config": rule,
                        "tenant_id":   dispatch_tenant,
                        "dry_run":     False,
                    },
                    queue="sync",
                )
                healed.append(repo_url)
            except Exception:
                logger.exception("sync_all dispatch error repo=%s", repo_url)
                errors.append(repo_url)

    logger.info("sync_all done healed=%d errors=%d", len(healed), len(errors))


# ── CLI sync task ───────────────────────────────────────────────────────────────

@celery.task(
    bind=True,
    name="app.tasks.etl.process_cli_sync_task",
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError, OSError),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    acks_late=True,
    track_started=True,
)
def process_cli_sync_task(
    self,
    *,
    datasource:   str,
    env:          str,
    file_path:    str,
    content:      str,
    fmt:          str = "yaml",
    tenant_id:    str = "default",
    dry_run:      bool = False,
    label:        str = "",
    submitted_by: str = "unknown",
) -> dict[str, Any]:
    """
    ETL pipeline driven by a directly supplied file content (CLI / CI push).

    Skips Stage 1 (no git webhook payload) and goes straight to Stage 2+3
    using a synthetic PayloadContext.
    """
    from celery.exceptions import SoftTimeLimitExceeded
    from app.monitoring.tracer import start_webhook_span

    task_id = self.request.id
    _bind_log_context(
        task_id=task_id, datasource=datasource,
        env=env, dry_run=dry_run, submitted_by=submitted_by,
    )

    span = start_webhook_span(task_id=task_id, platform="cli", datasource=datasource)

    logger.info(
        "cli_sync_start id=%s datasource=%s env=%s file=%s dry_run=%s by=%s",
        task_id, datasource, env, file_path, dry_run, submitted_by,
    )

    _t0 = time.perf_counter()

    try:
        # Resolve rule config for this tenant+datasource from the bundle.
        from app.pipeline.schema_utils import rule_config_for_datasource
        rule_config = rule_config_for_datasource(tenant_id, datasource)
        if rule_config is None:
            return fail(task_id, f"datasource {datasource!r} not found in bundle for tenant {tenant_id!r}")

        if dry_run:
            rule_config = {**rule_config, "dry_run": True}

        # Synthetic context — no git platform, no commit SHA.
        ctx = PayloadContext(
            platform="cli",
            repo_url="local",
            ref=f"refs/heads/cli/{env}",
            branch=f"cli/{env}",
            commit_sha=label or "cli-push",
            tag=None,
            pr_number=None,
            rule_config=rule_config,
            parsed_payload={},
        )

        # Stage 2 — ETL directly on the provided content.
        self.update_state(state="PROGRESS", meta={"stage": "etl", "file": file_path})
        _t_etl = time.perf_counter()

        from app.pipeline.stage_etl import run_stage_etl_content
        etl = run_stage_etl_content(
            ctx=ctx,
            file_path=file_path,
            content=content,
            fmt=fmt,
            env=env,
        )
        _observe("etl", time.perf_counter() - _t_etl)

        if etl.errors:
            return fail(task_id, f"etl errors: {etl.errors}")

        if dry_run:
            return ok(
                task_id=task_id,
                duration=round(time.perf_counter() - _t0, 3),
                dry_run=True,
                files=[
                    {
                        "file": f.file_path,
                        "env": f.env,
                        "keys": len(f.flat_data),
                        "validation": f.validation,
                    }
                    for f in etl.files
                ],
                skipped=etl.skipped,
            )

        # Stage 3 — Sync.
        self.update_state(state="PROGRESS", meta={"stage": "sync", "file": file_path})
        _t_sync = time.perf_counter()

        from app.pipeline.stage_sync import run_stage_sync, sync_result_to_dict
        sync_results = run_stage_sync(
            ctx=ctx,
            etl=etl,
            datasource=datasource,
            tenant_id=tenant_id,
            total_sources=1,
        )
        _observe("sync", time.perf_counter() - _t_sync)

        total_written = sum(r.written for r in sync_results)
        total_pruned  = sum(r.pruned  for r in sync_results)
        had_error     = any(r.status == "error" for r in sync_results)

        result = ok(
            task_id=task_id,
            duration=round(time.perf_counter() - _t0, 3),
            dry_run=False,
            datasource=datasource,
            env=env,
            files=[sync_result_to_dict(r) for r in sync_results],
            total_written=total_written,
            total_pruned=total_pruned,
        )
        if had_error:
            result["status"] = "partial_error"
        return result

    except SoftTimeLimitExceeded:
        logger.error("cli_sync soft time limit exceeded task=%s", task_id)
        return fail(task_id, "task exceeded time limit")
    except Exception as exc:
        logger.exception("cli_sync error task=%s", task_id)
        return fail(task_id, str(exc))
    finally:
        if span:
            span.end()
        _clear_log_context()
