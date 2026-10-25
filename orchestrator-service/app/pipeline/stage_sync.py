from __future__ import annotations

import fnmatch
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from app.pipeline.models import PayloadContext, ETLResult, SyncResult
from app.pipeline.change_logger import emit_field_changes
from app.tasks.payload import extract_prune_config
from app.utils.enums.sync_mode import SyncMode, parse_sync_mode

logger = logging.getLogger(__name__)


def _m():
    """Lazy metrics accessor — returns None when monitoring not initialised."""
    try:
        from app.monitoring import get_metrics
        return get_metrics()
    except Exception:
        return None

# One sync at a time per environment of a datasource. The lock is a lease: a
# worker that dies cannot hold the target for good.
LOCK_LEASE_SECONDS = 600   # the Celery task time limit
LOCK_WAIT_SECONDS = 300


class TargetBusy(RuntimeError):
    """Another sync of the same datasource and environment is still writing."""


@contextmanager
def target_locks(tenant_id: str, datasource: str, envs: Iterable[str]) -> Iterator[None]:
    """Hold the lock of every environment written, taken in a fixed order so
    two syncs cannot wait on each other. Without Redis there is no lock."""
    from app.ui import ledger

    held = []
    try:
        for env in sorted(set(envs)):
            lock = ledger._redis().lock(f"vt:sync-lock:{tenant_id}:{datasource}:{env}",
                                        timeout=LOCK_LEASE_SECONDS, blocking_timeout=LOCK_WAIT_SECONDS)
            try:
                acquired = lock.acquire()
            except Exception as exc:  # Redis unreachable: the broker is too
                logger.warning("sync lock for %s/%s not taken: %s", datasource, env, exc)
                continue
            if not acquired:
                raise TargetBusy(f"another sync of {datasource}/{env} is still running "
                                 f"after {LOCK_WAIT_SECONDS}s")
            held.append(lock)
        yield
    finally:
        for lock in reversed(held):
            try:
                lock.release()
            except Exception:
                pass  # the lease ran out: the lock is gone already


def _inventory(entries: dict[str, dict]) -> set[str]:
    return {key for entry in entries.values() for key in entry.get("desired", {})}


def _every_rule() -> list[tuple[str, dict]]:
    """(tenant, rule) of every tenant's rules, each with its datasource's
    config.cue settings laid over it: what shows where a rule writes."""
    from app.pipeline.datasource_settings import with_datasource_settings
    from app.ui import drift

    return [(tenant, with_datasource_settings(str(rule.get("datasource", "")), rule))
            for tenant, rules in drift.tenant_rules() for rule in rules]


def _keys_held_elsewhere(
    sink_cls: Any, rule_config: dict, space: str, seen: set[tuple[str, str, str]],
) -> set[str]:
    """The keys other datasources and tenants hold in the key space *space* of
    the store the rule writes to. A datasource name says nothing about where
    it writes: two datasources on the same Redis database with the same prefix
    and hash (or the same ZooKeeper servers and root, or Vercel project and
    targets) write the same keys, so one's stale key may be the other's live
    one. *seen* are the targets already counted."""
    from app.pipeline.sinks import sink_class
    from app.ui import ledger

    store = sink_cls.store(rule_config)
    held: set[str] = set()
    for tenant, other in _every_rule():
        datasource = str(other.get("datasource", ""))
        if sink_class(datasource) is not sink_cls or sink_cls.store(other) != store:
            continue
        for env in ledger.list_envs(tenant, datasource):
            if (tenant, datasource, env) in seen:
                continue
            seen.add((tenant, datasource, env))
            if sink_cls.key_space(other, tenant, env) == space:
                held |= _inventory(ledger.entries_of(ledger.get_target(tenant, datasource, env)))
    return held


def _stale_keys(
    tenant_id: str, datasource: str, rule_config: dict, sink_cls: Any, env: str,
    written: dict[str, tuple[str, dict]],
) -> tuple[set[str], dict[str, tuple[Any, str | None]]]:
    """The keys varTrack wrote to env before that nothing holds any more: the
    old inventory minus the new one. Returns them with the value (or secret
    digest) last written for each.

    *written* is this sync's files for env: inventory key -> (file path, flat
    data). The environment's other files keep their keys, and so do the
    environments that share env's key space (two that land on the same Redis
    hash, Vercel target or znode path write the same keys). Raises when the
    ledger cannot be read."""
    from app.ui import ledger

    target = ledger.get_target(tenant_id, datasource, env)
    old_entries = ledger.entries_of(target)
    pending: dict[str, dict] = (target or {}).get("pendingPrune") or {}

    rewritten = set(written) | {path for path, _ in written.values()}
    kept = {key: entry for key, entry in old_entries.items() if key not in rewritten}
    new = _inventory(kept) | {key for _, flat in written.values() for key in flat}

    space = sink_cls.key_space(rule_config, tenant_id, env)
    seen = {(tenant_id, datasource, env)}
    for other in ledger.list_envs(tenant_id, datasource):
        seen.add((tenant_id, datasource, other))
        if other != env and sink_cls.key_space(rule_config, tenant_id, other) == space:
            new |= _inventory(ledger.entries_of(ledger.get_target(tenant_id, datasource, other)))
    new |= _keys_held_elsewhere(sink_cls, rule_config, space, seen)

    stale = (_inventory(old_entries) | set(pending)) - new
    desired = (target or {}).get("desired") or {}
    digests = (target or {}).get("secretDigests") or {}
    expected = {
        key: ((desired[key], digests.get(key)) if key in desired
              else (pending[key].get("value"), pending[key].get("digest")))
        for key in stale
    }
    return stale, expected


@dataclass
class PruneOutcome:
    pruned:       int = 0
    pending:      list[str] | None = None   # None: prune did not run
    skipped:      list[str] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)
    error:        str = ""


def _prune_environment(
    sink: Any, rule_config: dict, prune_cfg: Any, tenant_id: str, datasource: str, env: str,
    written: dict[str, tuple[str, dict]],
) -> PruneOutcome | None:
    """Delete env's stale keys, once, after all its files were written.

    Prune is not a write's job: a sink deletes single keys (delete_keys) and
    this decides which, once, after every file is applied. A stale key that
    something else changed since varTrack wrote it is left alone and
    reported."""
    from app.ui import ledger

    sink_cls = type(sink)
    if not prune_cfg.enabled or not sink_cls.prunes_keys(rule_config):
        return None
    try:
        stale, expected = _stale_keys(tenant_id, datasource, rule_config, sink_cls, env, written)
    except Exception:
        logger.warning("prune: the ledger of %s/%s cannot be read - nothing is pruned", datasource, env,
                       exc_info=True)
        return None
    # Protected keys stay, and are no longer varTrack's.
    stale = {key for key in stale if not any(fnmatch.fnmatch(key, p) for p in prune_cfg.protection)}
    if not stale:
        return PruneOutcome(pending=[])
    keys = sorted(stale)
    if prune_cfg.dry_run:
        return PruneOutcome(pending=keys, would_delete=keys)

    sink.strict_reads = True  # a failed read is "can't tell", not "every key is gone"
    try:
        live = sink.read_values(keys, datasource, env)
    except Exception as exc:
        logger.warning("prune: %s/%s cannot be read (%s) - nothing is pruned", datasource, env, exc)
        return PruneOutcome(pending=keys)
    finally:
        sink.strict_reads = False

    to_delete: list[str] = []
    skipped: list[str] = []
    for key in keys:
        value, digest = expected[key]
        if value is None:
            state = "drifted"  # not known what varTrack wrote: not ours to delete
        else:
            state = ledger.key_state(value, live.get(key), digest)
        if state == "drifted":
            logger.info("prune: %s/%s %s was changed outside varTrack - not deleted", datasource, env, key)
            skipped.append(key)
        elif state != "missing":  # synced, or unreadable (write-only in the sink)
            to_delete.append(key)
    try:
        pruned = sink.delete_keys(to_delete, datasource=datasource, env=env) if to_delete else 0
    except Exception as exc:
        logger.exception("prune: %s/%s failed", datasource, env)
        return PruneOutcome(pending=to_delete, skipped=skipped, error=str(exc))
    return PruneOutcome(pruned=pruned, pending=[], skipped=skipped)


def prune_candidates(
    etl: ETLResult, datasource: str, tenant_id: str, rule_config: dict, repo_url: str,
) -> dict[str, list[str]]:
    """For a dry run: per environment, the keys a sync of *etl* would see as
    stale (before it reads the sink to see whether something else changed
    them). Nothing is connected to; empty when the rule does not prune, the
    sink does not delete single keys, or the ledger cannot be read."""
    from app.pipeline.sinks import sink_class
    from app.pipeline.datasource_settings import with_datasource_settings
    from app.ui import ledger

    prune_cfg = extract_prune_config(rule_config)
    sink_cls = sink_class(datasource)
    rule_config = with_datasource_settings(datasource, rule_config)
    if sink_cls is None or not prune_cfg.enabled or not sink_cls.prunes_keys(rule_config):
        return {}
    written_by_env: dict[str, dict[str, tuple[str, dict]]] = {}
    for f in etl.files:
        if f.flat_data:
            written_by_env.setdefault(f.env, {})[ledger.inventory_key(repo_url, f.file_path)] = (f.file_path, f.flat_data)
    out: dict[str, list[str]] = {}
    for env, written in written_by_env.items():
        try:
            stale, _ = _stale_keys(tenant_id, datasource, rule_config, sink_cls, env, written)
        except Exception:
            logger.warning("prune: the ledger of %s/%s cannot be read - no candidates listed", datasource, env)
            continue
        keys = sorted(k for k in stale if not any(fnmatch.fnmatch(k, p) for p in prune_cfg.protection))
        if keys:
            out[env] = keys
    return out


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
    repo_url:      str | None = None,
) -> list[SyncResult]:
    """
    Write each transformed file to the MongoDB sink.

    Strategy decision:
      - Respects explicit sync_mode from rule_config.
      - Falls back to a size-based heuristic when sync_mode is UNSPECIFIED/AUTO.

    Prune runs after the writes, once per environment, if prune is configured
    in rule_config and no file failed (see _prune_environment).

    *repo_url* names the repository the files are recorded under in the
    ledger; it is the context's unless the caller says otherwise.
    """
    from app.pipeline.sinks import create_sink

    rule_config = ctx.rule_config
    prune_cfg   = extract_prune_config(rule_config)

    configured_mode = parse_sync_mode(rule_config.get("sync_mode", 0))

    results: list[SyncResult] = []
    repo = repo_url if repo_url is not None else ctx.repo_url
    # This sync's files per environment: inventory key -> (path, flat data).
    written_by_env: dict[str, dict[str, tuple[str, dict]]] = {}

    # Sink factory: datasource URL path selects the right sink implementation.
    # e.g. POST /webhooks/mongo → MongoSink
    #      POST /webhooks/redis → RedisSink
    #      POST /webhooks/s3   → S3Sink
    #
    # Construct the sink once before the loop, reuse for all writes,
    # close once after all files are done (single connection pool).
    files_iter = list(etl.files)
    # The sink is opened only once the environments are this sync's alone.
    with target_locks(tenant_id, datasource, [f.env for f in files_iter if f.flat_data]):
        sink = create_sink(datasource=datasource, rule_config=rule_config, tenant_id=tenant_id)

        # Rollback tracking: before each write we snapshot the current state of that
        # file so that a failure can restore prior values, not just delete new ones.
        # Each entry is (datasource, env, file_path, snapshot_docs).
        # snapshot_docs is empty when the file did not previously exist.
        written_files: list[tuple[str, str, str, list]] = []

        try:
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

                    # ── Snapshot current state before writing ─────────────────────
                    snapshot: list = []
                    if hasattr(sink, "snapshot_file_data"):
                        snapshot = sink.snapshot_file_data(datasource, etl_file.env, etl_file.file_path)

                    # ── Write to sink ─────────────────────────────────────────────
                    _old_values: dict[str, str] = {}
                    if etl_file.logger_fields:
                        from app.pipeline.sinks.base import Unreadable
                        _old_values = {
                            k: v for k, v in sink.read_values(
                                etl_file.logger_fields, datasource, etl_file.env
                            ).items() if not isinstance(v, Unreadable)
                        }

                    _t_write = time.perf_counter()
                    write_result = sink.write(
                        flat_data=etl_file.flat_data,
                        data=etl_file.data,
                        sync_mode=sync_mode,
                        datasource=datasource,
                        env=etl_file.env,
                        repo=ctx.repo_url,
                        branch=ctx.branch,
                        commit_sha=ctx.commit_sha,
                        file_path=etl_file.file_path,
                        tenant_id=tenant_id,
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
                    written_files.append((datasource, etl_file.env, etl_file.file_path, snapshot))
                    from app.ui import ledger
                    written_by_env.setdefault(etl_file.env, {})[
                        ledger.inventory_key(repo, etl_file.file_path)
                    ] = (etl_file.file_path, etl_file.flat_data)

                    # ── fire-and-forget write metrics ────────────────────────────
                    metrics = _m()
                    if metrics:
                        try:
                            metrics.observe_mongo_write(sync_mode.name, _write_elapsed)
                            metrics.inc_mongo_written(datasource, sync_mode.name, sync_result.written)
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

                    # ── Rollback: restore each file to its pre-write state ────────
                    for rb_datasource, rb_env, rb_file, rb_snapshot in written_files:
                        try:
                            if hasattr(sink, "restore_file_data"):
                                sink.restore_file_data(rb_datasource, rb_env, rb_file, rb_snapshot)
                                logger.info(
                                    "rollback: restored file=%s env=%s docs=%d",
                                    rb_file, rb_env, len(rb_snapshot),
                                )
                            else:
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
            else:
                # Every file was written (no break): now prune, once per environment.
                for env, written in written_by_env.items():
                    outcome = _prune_environment(
                        sink, rule_config, prune_cfg, tenant_id, datasource, env, written)
                    last = next((r for r in reversed(results) if r.env == env and r.status == "ok"), None)
                    if outcome is None or last is None:
                        continue
                    last.pruned = outcome.pruned
                    last.prune_pending = outcome.pending
                    last.prune_skipped = outcome.skipped
                    last.prune_would_delete = outcome.would_delete
                    last.prune_error = outcome.error
                    metrics = _m()
                    if metrics and outcome.pruned:
                        try:
                            metrics.inc_mongo_pruned(datasource, outcome.pruned)
                        except Exception:
                            pass
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
        **({"prune_pending": r.prune_pending} if r.prune_pending is not None else {}),
        **({"prune_skipped": r.prune_skipped} if r.prune_skipped else {}),
        **({"prune_would_delete": r.prune_would_delete} if r.prune_would_delete else {}),
        **({"prune_error": r.prune_error} if r.prune_error else {}),
    }
