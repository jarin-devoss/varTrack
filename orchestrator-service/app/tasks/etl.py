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
from app.tasks.result import ok, fail
from app.pipeline.schema_utils import rules_from_bundle_all
from app.dependencies import DependencyError
from app.pipeline.env_resolver import heal_branches
from app.pipeline.repo_scope import RepositoryNotAllowed
from app.pipeline.stage_payload import run_stage_payload
from app.pipeline.stage_etl import run_stage_etl
from app.pipeline.stage_sync import run_stage_sync, sync_result_to_dict
from app.pipeline.models import PayloadContext, ETLResult
from app.ui import ledger
from app.ui import recorder as ui_recorder
from app.ui.ledger import TRIGGER_HEADER, trigger_from_headers

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
    datasource:  str = "",
    tenant_id:   str = "default",
) -> dict[str, Any]:
    """Build a dry-run simulation report from Stage 2 output (no writes)."""
    from app.pipeline.stage_sync import prune_candidates

    would_delete = prune_candidates(etl, datasource, tenant_id, ctx.rule_config, ctx.repo_url) if datasource else {}
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
            "schema":     f.schema,
            "schema_source": f.schema_source,
            **({"schema_note": f.schema_note} if f.schema_note else {}),
            "sample":     dict(list(f.flat_data.items())[:5]),
        })
    for skipped in etl.skipped:
        files_report.append({"file": skipped, "status": "skipped"})
    for failed in etl.errors:
        files_report.append({"file": failed, "status": "error", "error": etl.failure(failed)})

    outcome = fail if etl.errors else ok
    return outcome(task_id, f"failed: {len(etl.errors)} file(s) could not be processed" if etl.errors
                   else "dry_run_complete", **{
        "dry_run":     True,
        "received_at": received_at,
        "platform":    ctx.platform,
        "branch":      ctx.branch,
        "commit_sha":  ctx.commit_sha,
        "summary": {
            "total_files": len(etl.files),
            "skipped":     len(etl.skipped),
            "errors":      len(etl.errors),
            "total_keys":  total_keys,
        },
        "files": files_report,
        "prune": {"would_delete": would_delete},
        "schema_commit": etl.schema_commit,
    })


def _failed_message(etl: Any) -> str:
    """"failed: 2 file(s) could not be processed (a.yaml: ValueError: …; …)"."""
    detail = "; ".join(f"{path}: {etl.failure(path)}" for path in etl.errors)
    return f"failed: {len(etl.errors)} file(s) could not be processed ({detail})"


def _error_files(etl: Any) -> list[dict[str, Any]]:
    return [{"file": path, "status": "error", "error": etl.failure(path)} for path in etl.errors]


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
    ui_task = None

    try:
        # ── Stage 1 · PAYLOAD ─────────────────────────────────────────────────
        self.update_state(state="PROGRESS", meta={"stage": "payload", "progress": 0.05})
        try:
            ctx = run_stage_payload(
                raw_payload=raw_payload,
                platform=platform,
                rule_config=rule_config,
                datasource=datasource,
                tenant_id=tenant_id,
            )
        except RepositoryNotAllowed as exc:
            logger.warning("task=%s refused: %s (platform=%s datasource=%s)", task_id, exc, platform, datasource)
            return fail(task_id, str(exc))
        if ctx is None:
            return fail(task_id, f"no rule for platform={platform} datasource={datasource}")
        if not ctx.repo_url or not ctx.ref:
            return fail(task_id, "missing repo_url or ref in rule_config / payload")

        # A suspended rule takes no push and no heal; a sync started by hand
        # still runs.
        trigger = trigger_from_headers(headers)
        if ctx.rule_config.get("suspend") and trigger in ("webhook", "self_heal"):
            logger.info("process_webhook task=%s: %s/%s is suspended — %s skipped",
                        task_id, platform, datasource, trigger)
            return ok(task_id, f"{datasource} is suspended: {trigger} skipped")

        logger.info("stage=payload ref=%s branch=%s commit=%s", ctx.ref, ctx.branch, ctx.commit_sha)

        # ── Repo-level overrides (vartrack.json) ──────────────────────────────
        # If the repo contains a vartrack.json at its root, its settings
        # override the matching central rule_config keys.  Infrastructure and
        # schema-registry settings are never overridden.
        from app.pipeline.repo_overrides import RepoConfigError, apply_repo_overrides
        try:
            ctx = apply_repo_overrides(ctx, tenant_id)
        except RepoConfigError as exc:
            # An invalid vartrack.json: stop, and say why.
            logger.warning("process_webhook task=%s: %s", task_id, exc)
            return fail(task_id, str(exc))

        # A dry run is the task's own argument (the UI's Dry run, the dry-run webhook): never the rule's.
        ctx.dry_run = dry_run

        ui_task = ui_recorder.task_started(
            task_id,
            tenant=tenant_id,
            platform=platform,
            datasource=datasource,
            trigger=trigger,
            repo_url=ctx.repo_url,
            ref=ctx.branch or ctx.ref,
            revision=ctx.commit_sha,
            dry_run=dry_run,
            parsed_payload=ctx.parsed_payload,
            preview=dry_run and ledger.is_preview(headers),
        )

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

        if etl.errors and not etl.files:
            # Every file of the push failed: that is not "no files".
            message = _failed_message(etl)
            logger.error("stage=etl failed: %s", message)
            ui_recorder.task_errored(ui_task, tenant=tenant_id, error=message)
            return fail(task_id, message, dry_run=dry_run, files=_error_files(etl), errors=len(etl.errors),
                        schema_commit=etl.schema_commit)

        if not etl.files and not etl.skipped:
            logger.info("stage=etl result=no_files")
            ui_recorder.task_finished(ui_task, tenant=tenant_id, message="no matching files in this push")
            return ok(task_id, "no_files", **{"dry_run": dry_run, "files": [], "schema_commit": etl.schema_commit})

        logger.info(
            "stage=etl transformed=%d skipped=%d errors=%d",
            len(etl.files), len(etl.skipped), len(etl.errors),
        )

        # Dry-run exits after ETL — no writes to any datasource.
        if dry_run:
            report = _build_dry_run_report(task_id, ctx, etl, received_at, datasource, tenant_id)
            ui_recorder.task_finished(ui_task, tenant=tenant_id, dry_run_files=report.get("files", []),
                                      would_prune=(report.get("prune") or {}).get("would_delete", {}))
            return report

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

        failed  = [r for r in sync_results if r.status not in ("ok", "empty", "skipped")]
        overall = "completed" if not failed else (
            f"failed: {len(failed)} of {len(sync_results)} file(s): "
            + "; ".join(f"{r.file_path}: {r.error or r.status}" for r in failed))
        ui_recorder.task_finished(ui_task, tenant=tenant_id, etl=etl, results=sync_results, message=overall if failed else "")

        logger.info(
            "stage=sync written=%d errors=%d duration_ms=%.0f",
            sum(r.written for r in sync_results),
            len(failed),
            (time.perf_counter() - _t0) * 1000,
        )

        # A task that did not do what the push asked is not a success: the files that were written
        # stay listed as written, the ones that were not say why.
        return (fail if failed else ok)(task_id, overall, **{
            "files":  [sync_result_to_dict(r) for r in sync_results],
            "errors": len(failed),
            "schema_commit": etl.schema_commit,
        })

    except DependencyError as exc:
        # Something the sync needs could not be read: the task failed, and says what and why.
        span.end(exc)
        message = f"{exc.dependency} unavailable: {exc.reason}"
        logger.error("task=%s failed: %s", task_id, message)
        ui_recorder.task_errored(ui_task, tenant=tenant_id, error=message)
        return fail(task_id, message, dependency=exc.dependency)

    except SoftTimeLimitExceeded:
        span.end()
        logger.warning("task=%s hit soft time limit — will retry", task_id)
        # Left RUNNING, the watcher would wait on this sync for good. A retry
        # records itself as running again under the same id.
        ui_recorder.task_errored(ui_task, tenant=tenant_id, error="hit the time limit — retrying once")
        raise self.retry(countdown=30, max_retries=1)

    except Exception as exc:
        span.end(exc)
        logger.exception("task=%s unhandled error: %s", task_id, exc)
        ui_recorder.task_errored(ui_task, tenant=tenant_id, error=str(exc))
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
    tenant_id:   str = "",
) -> dict[str, Any]:
    """Refresh the schema registry when a tenant's schema repository receives a push.

    The push says which repository it came from; the tenant is the one an operator registered
    that repository for. What is re-read is that registered repository, on its registered
    branch and with its registered credential: the pushed repository is never cloned as
    anyone's schema repository, and no token comes from the webhook's headers. *tenant_id*, when
    the caller names one, must be that tenant."""
    task_id = self.request.id

    from app.schema_registry.manager import get_schema_manager
    mgr    = get_schema_manager()
    tenant = mgr.tenant_for_repo(repo)
    if tenant is None:
        logger.warning("schema refresh task=%s refused: %s is no tenant's schema repository", task_id, repo)
        return fail(task_id, "schema_repository_not_registered", repo=repo)
    if tenant_id and tenant_id != tenant:
        logger.warning("schema refresh task=%s refused: %s is the schema repository of %s, not of %s",
                       task_id, repo, tenant, tenant_id)
        return fail(task_id, "schema_repository_belongs_to_another_tenant", repo=repo)
    if branch and branch != mgr.registered_branch(tenant):
        logger.info("schema refresh task=%s tenant=%s: push to %s, the registry follows %s — ignored",
                    task_id, tenant, branch, mgr.registered_branch(tenant))
        return ok(task_id, "schema_branch_not_followed", tenant_id=tenant, repo=repo, branch=branch)
    logger.info("schema refresh task=%s tenant=%s repo=%s", task_id, tenant, repo)

    mgr.invalidate(tenant)
    local_path = mgr.get_or_clone(tenant)
    cue_files  = list(local_path.rglob("*.cue"))
    return ok(task_id, "schema_refreshed", **{
        "tenant_id":  tenant,
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
    rules:      list[dict],
    tenant_id:  str | None = None,
    datasource: str | None = None,
    platform:   str | None = None,
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

    heal_rules = [r for r in rules if r.get("self_heal", False) and not r.get("suspend")]
    # A heal requested for one datasource (the watcher saw it drift) re-syncs
    # only that datasource's rules; the periodic beat passes neither and heals all.
    if datasource:
        heal_rules = [r for r in heal_rules if r.get("datasource") == datasource]
    if platform:
        heal_rules = [r for r in heal_rules if r.get("platform") == platform]
    if not heal_rules:
        logger.info("sync_all task=%s: no self_heal rules, done", task_id)
        return

    from app.pipeline.git_extractor import GitExtractor
    extractor = GitExtractor()

    def _resolve_sha(item: tuple[dict, str]) -> tuple[dict, str, str | None]:
        """Resolve the HEAD SHA for a rule's repo/branch (runs in thread pool)."""
        rule, branch = item
        repo_url = rule.get("repo_url", "")
        from app.pipeline.git_credentials import credential_for
        token    = credential_for(rule, repo_url)
        try:
            sha = extractor.get_commit_sha(repo_url, branch, token=token)
            return rule, branch, sha
        except Exception:
            logger.exception("sync_all sha_resolve error repo=%s branch=%s", repo_url, branch)
            return rule, branch, None

    # Every branch that feeds an environment gets healed, not only the
    # rule's default branch: branch_map {main: production, develop: staging}
    # heals both production and staging.
    work: list[tuple[dict, str]] = []
    for rule in heal_rules:
        work.extend((rule, b) for b in heal_branches(rule))

    healed, errors = [], []

    # Parallelise git ls-remote calls (I/O-bound).
    # max_workers=8: enough parallelism without overwhelming the git host.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sync_sha") as pool:
        futures = {pool.submit(_resolve_sha, item): item for item in work}

        for future in as_completed(futures):
            rule, branch, sha = future.result()
            if sha is None:
                errors.append(rule.get("repo_url", "?"))
                continue

            repo_url = rule.get("repo_url", "")
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
                        "headers":     {TRIGGER_HEADER: "self_heal"},
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
    revision:     str = "",
    platform:     str = "",
) -> dict[str, Any]:
    """
    ETL pipeline driven by a directly supplied file content (CLI / CI push),
    or by the rule's file at a branch or tag (*revision*).

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

    # The CLI is a client, not a Git platform: the sync belongs to the rule
    # for this datasource, and carries that rule's platform once it is known.
    span = start_webhook_span(task_id=task_id, platform="", datasource=datasource, event_type="cli")

    logger.info(
        "cli_sync_start id=%s datasource=%s env=%s file=%s dry_run=%s by=%s",
        task_id, datasource, env, file_path, dry_run, submitted_by,
    )

    _t0 = time.perf_counter()
    ui_task = None

    try:
        # Resolve rule config for this tenant+datasource from the bundle.
        from app.pipeline.schema_utils import AmbiguousRule, rule_config_for_datasource
        try:
            rule_config = rule_config_for_datasource(tenant_id, datasource, platform)
        except AmbiguousRule as exc:
            return fail(task_id, str(exc), tenant_id=tenant_id)
        if rule_config is None:
            what = f"platform {platform!r} and datasource {datasource!r}" if platform else f"datasource {datasource!r}"
            return fail(task_id, f"{what} not found in bundle for tenant {tenant_id!r}", tenant_id=tenant_id)

        platform = str(rule_config.get("platform", ""))

        if revision:
            from app.cli_ops import detect_format, file_at_revision
            try:
                file_path, content = file_at_revision(rule_config, env, revision)
            except ValueError as exc:
                return fail(task_id, str(exc), tenant_id=tenant_id)
            fmt = detect_format(file_path, None)

        # Write the pushed file as the rule's own file, so the sinks replace
        # what a Git push of it wrote rather than storing a second copy.
        from app.cli_ops import rule_file_for
        target = rule_file_for(rule_config, file_path)
        if target != file_path:
            logger.info("cli_sync file=%s is the rule's %s", file_path, target)
            file_path = target
        if span:
            span.set_attr("webhook.platform", platform)

        task_repo_url = rule_config.get("repo_url", "local") if revision else "local"
        ui_task = ui_recorder.task_started(
            task_id,
            tenant=tenant_id,
            platform=platform,
            datasource=datasource,
            trigger="cli",
            repo_url=task_repo_url,
            ref=revision or f"cli/{env}",
            revision=label or revision or "cli-push",
            dry_run=dry_run,
            env=env,
            submitted_by=submitted_by,
        )

        # Synthetic context — the rule's platform, but no Git push: no commit SHA.
        ctx = PayloadContext(
            platform=platform,
            repo_url="local",
            ref=f"refs/heads/cli/{env}",
            branch=f"cli/{env}",
            commit_sha=label or "cli-push",
            tag=None,
            pr_number=None,
            rule_config=rule_config,
            parsed_payload={},
            dry_run=dry_run,
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
            tenant_id=tenant_id,
        )
        _observe("etl", time.perf_counter() - _t_etl)

        if etl.errors:
            message = _failed_message(etl)
            ui_recorder.task_errored(ui_task, tenant=tenant_id, error=message)
            return fail(task_id, message, tenant_id=tenant_id)

        if dry_run:
            from app.pipeline.stage_sync import prune_candidates
            ui_recorder.task_finished(ui_task, tenant=tenant_id, dry_run_files=[
                {"file": f.file_path, "env": f.env, "keys": len(f.flat_data), "validation": f.validation,
                 "schema": f.schema, "schema_source": f.schema_source}
                for f in etl.files
            ])
            return ok(
                task_id=task_id,
                tenant_id=tenant_id,
                duration=round(time.perf_counter() - _t0, 3),
                dry_run=True,
                files=[
                    {
                        "file": f.file_path,
                        "env": f.env,
                        "keys": len(f.flat_data),
                        "validation": f.validation,
                        "schema": f.schema,
                        "schema_source": f.schema_source,
                        **({"schema_note": f.schema_note} if f.schema_note else {}),
                    }
                    for f in etl.files
                ],
                skipped=etl.skipped,
                prune={"would_delete": prune_candidates(etl, datasource, tenant_id, rule_config, task_repo_url)},
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
            repo_url=task_repo_url,  # the ledger records the files under the task's
        )
        _observe("sync", time.perf_counter() - _t_sync)

        ui_recorder.task_finished(ui_task, tenant=tenant_id, etl=etl, results=sync_results)
        total_written = sum(r.written for r in sync_results)
        total_pruned  = sum(r.pruned  for r in sync_results)
        had_error     = any(r.status == "error" for r in sync_results)

        result = ok(
            task_id=task_id,
                tenant_id=tenant_id,
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
        ui_recorder.task_errored(ui_task, tenant=tenant_id, error="task exceeded time limit")
        return fail(task_id, "task exceeded time limit", tenant_id=tenant_id)
    except Exception as exc:
        logger.exception("cli_sync error task=%s", task_id)
        ui_recorder.task_errored(ui_task, tenant=tenant_id, error=str(exc))
        return fail(task_id, str(exc), tenant_id=tenant_id)
    finally:
        if span:
            span.end()
        _clear_log_context()
