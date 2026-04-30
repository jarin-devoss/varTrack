from __future__ import annotations

import logging
import time

from app.pipeline.models import PayloadContext, ETLResult, SyncResult
from app.pipeline.change_logger import emit_field_changes
from app.tasks.payload import extract_prune_config
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)


def _m():
    """Lazy metrics accessor — returns None when monitoring not initialised."""
    try:
        from app.monitoring import get_metrics
        return get_metrics()
    except Exception:
        return None

def _decide_sync_mode(key_count: int, forced_mode: SyncMode) -> SyncMode:
    """
    Select the MongoDB write strategy.

    Explicit forced_mode (anything except UNSPECIFIED / AUTO) is always
    honoured.  Otherwise a key-count heuristic is applied:
      > 500 keys  →  GIT_SMART_REPAIR  (read + diff, skips unchanged keys)
      ≤ 500 keys  →  GIT_UPSERT_ALL   (simple bulk upsert)
    """
    if forced_mode not in (SyncMode.UNSPECIFIED, SyncMode.AUTO):
        return forced_mode
    return SyncMode.GIT_SMART_REPAIR if key_count > 500 else SyncMode.GIT_UPSERT_ALL


def run_stage_sync(
    ctx:           PayloadContext,
    etl:           ETLResult,
    datasource:    str,
    tenant_id:     str,
    total_sources: int,
) -> list[SyncResult]:
    """
    Write each transformed file to the MongoDB sink.

    Strategy decision:
      - Respects explicit sync_mode from rule_config.
      - Falls back to a size-based heuristic when sync_mode is UNSPECIFIED/AUTO.

    Prune pass is applied after writes if prune is configured in rule_config.
    """
    from app.pipeline.sinks import create_sink

    rule_config = ctx.rule_config
    prune_cfg   = extract_prune_config(rule_config)

    try:
        configured_mode = SyncMode(int(rule_config.get("sync_mode", 0)))
    except (ValueError, TypeError):
        configured_mode = SyncMode.UNSPECIFIED

    results: list[SyncResult] = []

    # Sink factory: datasource URL path selects the right sink implementation.
    # e.g. POST /webhooks/mongo → MongoSink
    #      POST /webhooks/redis → RedisSink
    #      POST /webhooks/s3   → S3Sink
    #
    # Construct the sink once before the loop, reuse for all writes,
    # close once after all files are done (single connection pool).
    sink = create_sink(datasource=datasource, rule_config=rule_config, tenant_id=tenant_id)

    # Application-level rollback: track every (datasource, env, file_path) tuple
    # written successfully.  On any write failure, delete_file_data() is called
    # for each previously committed file to restore the pre-deployment state.
    # Remaining un-attempted files are marked "aborted".
    #
    # Limitation: this removes newly-written documents but does NOT restore the
    # *previous* versions of pre-existing keys — that requires full MongoDB
    # client-session transactions (replica set required).
    written_files: list[tuple[str, str, str]] = []  # (datasource, env, file_path)

    try:
        files_iter = list(etl.files)

        for idx, etl_file in enumerate(files_iter):
            sync_result = SyncResult(
                file_path=etl_file.file_path,
                env=etl_file.env,
                status="ok",
                root_key=etl_file.root_key,
            )

            if not etl_file.flat_data:
                sync_result.status = "empty"
                results.append(sync_result)
                continue

            try:
                # ── Strategy decision ─────────────────────────────────────────
                sync_mode = _decide_sync_mode(len(etl_file.flat_data), configured_mode)

                # ── Write to sink ─────────────────────────────────────────────
                _old_values: dict[str, str] = {}
                if etl_file.logger_fields:
                    _old_values = sink.read_values(
                        etl_file.logger_fields, datasource, etl_file.env
                    )

                _t_write = time.perf_counter()
                write_result = sink.write(
                    flat_data=etl_file.flat_data,
                    sync_mode=sync_mode,
                    datasource=datasource,
                    env=etl_file.env,
                    repo=ctx.repo_url,
                    branch=ctx.branch,
                    commit_sha=ctx.commit_sha,
                    file_path=etl_file.file_path,
                    prune=prune_cfg.enabled,
                    prune_last=prune_cfg.last,
                    prune_protection=rule_config.get("prune_protection", []),
                    dry_run_prune=prune_cfg.dry_run,
                    total_sources=total_sources,
                )
                _write_elapsed = time.perf_counter() - _t_write

                if etl_file.logger_fields:
                    emit_field_changes(
                        logger_fields=etl_file.logger_fields,
                        secret_fields=etl_file.secret_fields,
                        old_values=_old_values,
                        new_values=etl_file.flat_data,
                        datasource=datasource,
                        env=etl_file.env,
                        file_path=etl_file.file_path,
                    )

                sync_result.sync_mode = sync_mode.name
                sync_result.written   = write_result.get("written", 0)
                sync_result.pruned    = write_result.get("pruned", 0)
                written_files.append((datasource, etl_file.env, etl_file.file_path))

                # ── fire-and-forget write metrics ────────────────────────────
                metrics = _m()
                if metrics:
                    try:
                        metrics.observe_mongo_write(sync_mode.name, _write_elapsed)
                        metrics.inc_mongo_written(datasource, sync_mode.name, sync_result.written)
                        metrics.inc_mongo_pruned(datasource, sync_result.pruned)
                        metrics.inc_etl_file("ok")
                    except Exception:
                        pass

            except Exception as exc:
                logger.exception("sync error file=%s — rolling back %d previously written file(s)",
                                 etl_file.file_path, len(written_files))
                sync_result.status = "error"
                sync_result.error  = str(exc)
                results.append(sync_result)

                metrics = _m()
                if metrics:
                    try:
                        metrics.inc_mongo_error("write")
                        metrics.inc_etl_file("error")
                    except Exception:
                        pass

                # ── Application-level rollback ────────────────────────────────
                for rb_datasource, rb_env, rb_file in written_files:
                    try:
                        sink.delete_file_data(rb_datasource, rb_env, rb_file)
                        logger.info("rollback: deleted file=%s env=%s", rb_file, rb_env)
                    except Exception:
                        logger.exception("rollback failed for file=%s env=%s", rb_file, rb_env)
                        if _m():
                            try:
                                _m().inc_mongo_error("rollback")
                            except Exception:
                                pass

                # Mark all remaining un-attempted files as aborted
                for remaining in files_iter[idx + 1:]:
                    results.append(SyncResult(
                        file_path=remaining.file_path,
                        env=remaining.env,
                        status="aborted",
                        root_key=remaining.root_key,
                        error="aborted due to earlier write failure + rollback",
                    ))
                break

            results.append(sync_result)
    finally:
        sink.close()

    # Skipped files from ETL become empty sync results
    for skipped_path in etl.skipped:
        results.append(SyncResult(
            file_path=skipped_path,
            env="unknown",
            status="skipped",
        ))

    return results


def sync_result_to_dict(r: SyncResult) -> dict:
    return {
        "file":      r.file_path,
        "env":       r.env,
        "status":    r.status,
        "sync_mode": r.sync_mode,
        "written":   r.written,
        "pruned":    r.pruned,
        "root_key":  r.root_key,
        **({"error": r.error} if r.error else {}),
    }
