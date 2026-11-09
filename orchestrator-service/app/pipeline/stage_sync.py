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
def target_locks(tenant_id: str, datasource: str, envs: Iterable[str],
                 rule_config: dict | None = None) -> Iterator[None]:
    """Hold the lock of every place written, taken in a fixed order so two syncs
    cannot wait on each other. The lock is keyed by the destination, not the tenant:
    two writers of one place meet whoever they act for. Without Redis there is no lock."""
    from app.pipeline.destinations import destination_of
    from app.ui import ledger

    # One lock per place, not per environment: environments that share a place (a destination_template
    # without {env}, several files of one push) would otherwise take the same lock twice and wait on themselves.
    by_place: dict[str, str] = {}
    for env in sorted(set(envs)):
        place = (destination_of(datasource, rule_config, tenant_id, env) if rule_config is not None else "")             or f"{tenant_id}:{env}"
        by_place.setdefault(place, env)
    held = []
    try:
        for place, env in sorted(by_place.items()):
            lock = ledger._redis().lock(f"vt:sync-lock:{datasource}:{place}",
                                        timeout=LOCK_LEASE_SECONDS, blocking_timeout=LOCK_WAIT_SECONDS)
            try:
                acquired = lock.acquire()
            except Exception as exc:  # Redis unreachable: the broker is too
                from app import dependencies

                dependencies.down("redis", f"the sync lock for {datasource}/{env} cannot be taken: {exc}")
                if dependencies.production():
                    # Two tenants (or two replicas of one) writing a place must not be allowed to
                    # race because the thing that keeps them apart is down.
                    raise dependencies.LockUnavailable(
                        f"the sync lock for {datasource}/{env} cannot be taken: {exc}") from None
                logger.warning("sync lock for %s/%s not taken (no lock outside production): %s",
                               datasource, env, exc)
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


def _check_destinations(tenant_id: str, datasource: str, rule_config: dict, envs: Iterable[str]) -> None:
    """Refuse, before anything is opened or written, a sync to a place that is not the
    tenant's (tenants.may_write). The error names the destination and what is allowed."""
    from app import tenants
    from app.pipeline.destinations import destination_of

    for env in sorted(set(envs)):
        tenants.may_write(tenant_id, datasource, destination_of(datasource, rule_config, tenant_id, env))


def _inventory(entries: dict[str, dict]) -> set[str]:
    return {key for entry in entries.values() for key in entry.get("desired", {})}


def key_space_id(sink_cls: Any, rule_config: dict, tenant_id: str, env: str) -> str:
    """Where a sync of *env* puts its keys, as one string: the sink type, the
    store (server) and the key space inside it. Two targets with the same id
    write the same keys. Empty for a sink that does not delete single keys,
    which has no key space to share."""
    from app.pipeline.sinks.settings import for_rule

    settings = for_rule(rule_config)
    if not sink_cls.prunes_keys(settings, rule_config):
        return ""
    return "|".join((sink_cls.__name__, str(sink_cls.store(settings, rule_config)),
                     str(sink_cls.key_space(settings, rule_config, tenant_id, env))))


def _recorded_key_space(sink: Any, rule_config: dict, tenant_id: str, env: str) -> str:
    """key_space_id for the ledger. Noting where keys went is no reason to fail
    a write that already happened."""
    try:
        return key_space_id(type(sink), rule_config, tenant_id, env)
    except Exception:
        logger.warning("key space of %s/%s not recorded", tenant_id, env, exc_info=True)
        return ""


def _tenant_ids() -> list[str]:
    """The tenants registered with this process: a local list, nothing is cloned."""
    from app.schema_registry.manager import get_schema_manager

    return list(get_schema_manager().list_tenant_ids())


def _every_rule() -> list[tuple[str, dict]]:
    """(tenant, rule) of every tenant's rules: where each writes is its datasource's settings
    and the rule's own options (key_space_id reads both).

    Only for targets recorded before they carried their key space: it rebuilds
    every tenant's rules, which fails or comes back empty for a tenant whose
    schema repository cannot be read."""
    from app.ui import drift

    return [(tenant, rule) for tenant, rules in drift.tenant_rules() for rule in rules]


def _keys_held_elsewhere(
    sink_cls: Any, rule_config: dict, space: str, seen: set[tuple[str, str, str]],
    tenant_id: str, sharers: list[str] | None = None,
) -> set[str]:
    """The keys other datasources and tenants hold in the key space *space*
    (a key_space_id) of the store the rule writes to. A datasource name says
    nothing about where it writes: two datasources on the same Redis database
    with the same prefix and hash (or the same ZooKeeper servers and root, or
    …) write the same keys, so one's stale key may be
    the other's live one. *seen* are the targets already counted; *sharers*,
    when given, collects "tenant/datasource/env" of each target that holds keys
    there.

    Who holds what is read from the records each sync leaves (the key space it
    wrote to), not rebuilt from the rules as they are now. A record made before
    targets carried their key space is placed by the rules instead."""
    from app.pipeline.sinks import sink_class
    from app.ui import ledger

    held: set[str] = set()
    legacy: set[tuple[str, str, str]] = set()
    for tenant in sorted({tenant_id, *_tenant_ids()}):
        for record in ledger.list_targets(tenant):
            target = (tenant, str(record.get("datasource", "")), str(record.get("env", "")))
            if target in seen:
                continue
            if "keySpace" not in record:
                if sink_class(target[1]) is sink_cls:
                    legacy.add(target)
                continue
            if record["keySpace"] != space:
                continue
            seen.add(target)
            keys = _inventory(ledger.entries_of(record))
            held |= keys
            if keys and sharers is not None:
                sharers.append("/".join(target))
    if legacy:
        held |= _keys_held_by_rules(sink_cls, space, seen, legacy, sharers)
    return held


def _keys_held_by_rules(
    sink_cls: Any, space: str, seen: set[tuple[str, str, str]],
    legacy: set[tuple[str, str, str]], sharers: list[str] | None,
) -> set[str]:
    """_keys_held_elsewhere for the *legacy* targets (recorded without a key
    space): which of them share *space* follows from the rules as they are
    now. Once every target has been written again this finds nothing to do."""
    from app.pipeline.sinks import sink_class
    from app.ui import ledger

    held: set[str] = set()
    for tenant, other in _every_rule():
        datasource = str(other.get("datasource", ""))
        if sink_class(datasource) is not sink_cls:
            continue
        for env in ledger.list_envs(tenant, datasource):
            target = (tenant, datasource, env)
            if target not in legacy or target in seen:
                continue
            seen.add(target)
            if key_space_id(sink_cls, other, tenant, env) == space:
                keys = _inventory(ledger.entries_of(ledger.get_target(tenant, datasource, env)))
                held |= keys
                if keys and sharers is not None:
                    sharers.append("/".join(target))
    return held


def _stale_keys(
    tenant_id: str, datasource: str, rule_config: dict, sink_cls: Any, env: str,
    written: dict[str, tuple[str, dict]], sharers: list[str] | None = None,
) -> tuple[set[str], dict[str, tuple[Any, str | None]]]:
    """The keys varTrack wrote to env before that nothing holds any more: the
    old inventory minus the new one. Returns them with the value (or secret
    digest) last written for each.

    *written* is this sync's files for env: inventory key -> (file path, flat
    data). The environment's other files keep their keys, and so do the
    environments that share env's key space (two that land on the same Redis
    hash or znode path write the same keys). *sharers* collects
    the other targets that hold keys in env's key space. Raises when the
    ledger cannot be read."""
    from app.ui import ledger

    target = ledger.get_target(tenant_id, datasource, env)
    old_entries = ledger.entries_of(target)
    pending: dict[str, dict] = (target or {}).get("pendingPrune") or {}

    rewritten = set(written) | {path for path, _ in written.values()}
    kept = {key: entry for key, entry in old_entries.items() if key not in rewritten}
    new = _inventory(kept) | {key for _, flat in written.values() for key in flat}

    space = key_space_id(sink_cls, rule_config, tenant_id, env)
    seen = {(tenant_id, datasource, env)}
    for other in ledger.list_envs(tenant_id, datasource):
        seen.add((tenant_id, datasource, other))
        if other != env and key_space_id(sink_cls, rule_config, tenant_id, other) == space:
            new |= _inventory(ledger.entries_of(ledger.get_target(tenant_id, datasource, other)))
    new |= _keys_held_elsewhere(sink_cls, rule_config, space, seen, tenant_id, sharers)

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
    error:        str = ""
    # Other targets holding keys in this environment's key space ("tenant/
    # datasource/env"); their keys are never pruned from here.
    shared_with:  list[str] = field(default_factory=list)


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
    from app.pipeline.sinks.settings import for_rule

    if not prune_cfg.enabled or not sink_cls.prunes_keys(for_rule(rule_config, datasource), rule_config):
        return None
    sharers: list[str] = []
    try:
        stale, expected = _stale_keys(tenant_id, datasource, rule_config, sink_cls, env, written, sharers)
    except Exception:
        logger.warning("prune: the ledger of %s/%s cannot be read - nothing is pruned", datasource, env,
                       exc_info=True)
        return None
    shared = sorted(set(sharers))
    # Protected keys stay, and are no longer varTrack's.
    stale = {key for key in stale if not any(fnmatch.fnmatch(key, p) for p in prune_cfg.protection)}
    if not stale:
        return PruneOutcome(pending=[], shared_with=shared)
    keys = sorted(stale)
    sink.strict_reads = True  # a failed read is "can't tell", not "every key is gone"
    try:
        live = sink.read_values(keys, datasource, env)
    except Exception as exc:
        logger.warning("prune: %s/%s cannot be read (%s) - nothing is pruned", datasource, env, exc)
        return PruneOutcome(pending=keys, shared_with=shared)
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
        return PruneOutcome(pending=to_delete, skipped=skipped, error=str(exc), shared_with=shared)
    return PruneOutcome(pruned=pruned, pending=[], skipped=skipped, shared_with=shared)


def prune_candidates(
    etl: ETLResult, datasource: str, tenant_id: str, rule_config: dict, repo_url: str,
) -> dict[str, list[str]]:
    """For a dry run: per environment, the keys a sync of *etl* would see as
    stale (before it reads the sink to see whether something else changed
    them). Nothing is connected to; empty when the rule does not prune, the
    sink does not delete single keys, or the ledger cannot be read."""
    from app.pipeline.sinks import sink_class
    from app.pipeline.sinks.settings import for_rule
    from app.ui import ledger

    prune_cfg = extract_prune_config(rule_config)
    sink_cls = sink_class(datasource)
    rule_config = {**rule_config, "datasource": rule_config.get("datasource") or datasource}
    if sink_cls is None or not prune_cfg.enabled or not sink_cls.prunes_keys(for_rule(rule_config), rule_config):
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


def _count(record: Any) -> None:
    """Run *record* on the metrics, when there are any: a metric never fails a sync."""
    metrics = _m()
    if metrics:
        try:
            record(metrics)
        except Exception:
            pass


def _sink_kind(sink: Any) -> str:
    """The datasource kind a sink serves ("redis", "linux_server" …): the label its metrics carry."""
    from app.pipeline.sinks import SINK_REGISTRY
    return next((kind for kind, cls in SINK_REGISTRY.items() if type(sink) is cls), type(sink).__name__.lower())


def _roll_back(sink: Any, written: list[tuple[str, str, str, list, SyncResult]], *,
               failed_file: str, reason: str) -> None:
    """A file of the sync failed: take back the files written before it, where the sink can.

    A sink that can_restore restores each file from the snapshot taken before it was written (or deletes a
    file that did not exist), and its result becomes ``rolled_back``. A sink that cannot keeps what it wrote:
    the result becomes ``partial`` and says so, the log says so, and nothing claims a deletion that did not
    happen. Either way the next sync, or self-heal, writes the desired state."""
    restorable = bool(getattr(sink, "can_restore", False))
    kind = _sink_kind(sink)
    for datasource, env, file_path, snapshot, res in written:
        if not restorable:
            _count(lambda m: m.inc_sink_not_rolled_back(kind))
            res.status = "partial"
            res.error = (f"not rolled back: {file_path} stays written in {env} ({type(sink).__name__} keeps no "
                         f"snapshot to restore); {failed_file} failed — the next sync or self-heal writes the "
                         f"desired state")
            logger.warning("rollback: not possible on %s — %s stays written in %s (%s failed: %s)",
                           type(sink).__name__, file_path, env, failed_file, reason)
            continue
        try:
            if hasattr(sink, "restore_file_data"):
                sink.restore_file_data(datasource, env, file_path, snapshot)
                logger.info("rollback: restored file=%s env=%s docs=%d", file_path, env, len(snapshot))
            else:
                sink.delete_file_data(datasource, env, file_path)
                logger.info("rollback: deleted file=%s env=%s", file_path, env)
            res.status, res.written = "rolled_back", 0
            res.error = f"rolled back: {failed_file} failed — {reason}"
        except Exception as exc:
            logger.exception("rollback failed for file=%s env=%s", file_path, env)
            res.status = "partial"
            res.error = (f"rollback failed: {file_path} stays written in {env} ({type(exc).__name__}); "
                         f"{failed_file} failed — the next sync or self-heal writes the desired state")
            _count(lambda m: (m.inc_sink_error(kind, "rollback"), m.inc_sink_not_rolled_back(kind)))


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
    written_envs = [f.env for f in files_iter if f.flat_data]
    _check_destinations(tenant_id, datasource, rule_config, written_envs)
    with target_locks(tenant_id, datasource, written_envs, rule_config):
        sink = create_sink(datasource=datasource, rule_config=rule_config, tenant_id=tenant_id)

        # Rollback tracking: before each write we snapshot the current state of that
        # file so that a failure can restore prior values, not just delete new ones.
        # Each entry is (datasource, env, file_path, snapshot_docs, its SyncResult).
        # snapshot_docs is empty when the file did not previously exist.
        written_files: list[tuple[str, str, str, list, SyncResult]] = []

        try:
            for idx, etl_file in enumerate(files_iter):
                sync_result = SyncResult(
                    file_path=etl_file.file_path,
                    env=etl_file.env,
                    status="ok",
                    root_key=etl_file.root_key,
                    validation=etl_file.validation,
                    schema=etl_file.schema,
                    schema_source=etl_file.schema_source,
                    schema_note=etl_file.schema_note,
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
                    sync_result.key_space = _recorded_key_space(sink, rule_config, tenant_id, etl_file.env)
                    sync_result.replaced  = write_result.get("replaced", 0) or 0
                    sync_result.replaced_by = write_result.get("strategy", "") if sync_result.replaced else ""
                    written_files.append((datasource, etl_file.env, etl_file.file_path, snapshot, sync_result))
                    from app.ui import ledger
                    written_by_env.setdefault(etl_file.env, {})[
                        ledger.inventory_key(repo, etl_file.file_path)
                    ] = (etl_file.file_path, etl_file.flat_data)

                    # ── fire-and-forget write metrics ────────────────────────────
                    kind = _sink_kind(sink)
                    _count(lambda m: (m.observe_sink_write(kind, sync_mode.name, _write_elapsed),
                                      m.inc_sink_written(kind, datasource, sync_mode.name, sync_result.written),
                                      m.inc_etl_file("ok")))

                except Exception as exc:
                    logger.exception("sync error file=%s — rolling back %d previously written file(s)",
                                     etl_file.file_path, len(written_files))
                    sync_result.status = "error"
                    sync_result.error  = str(exc)
                    results.append(sync_result)

                    kind = _sink_kind(sink)
                    _count(lambda m: (m.inc_sink_error(kind, "write"), m.inc_etl_file("error")))

                    # ── Rollback: put back what this sink can, say what it cannot ──
                    _roll_back(sink, written_files, failed_file=etl_file.file_path, reason=str(exc))

                    # Mark all remaining un-attempted files as aborted
                    for remaining in files_iter[idx + 1:]:
                        results.append(SyncResult(
                            file_path=remaining.file_path,
                            env=remaining.env,
                            status="aborted",
                            root_key=remaining.root_key,
                            error=f"aborted: {etl_file.file_path} failed before it was reached",
                        ))
                    break

                results.append(sync_result)
            else:
                # Every file was written (no break): now prune, once per environment. Not when
                # a file of this push could not even be read or parsed: what is missing from
                # an incomplete read is not what was removed.
                if etl.errors:
                    logger.warning("prune skipped: %d file(s) of this push failed before the sync (%s)",
                                   len(etl.errors), ", ".join(etl.errors))
                for env, written in (written_by_env.items() if not etl.errors else ()):
                    outcome = _prune_environment(
                        sink, rule_config, prune_cfg, tenant_id, datasource, env, written)
                    last = next((r for r in reversed(results) if r.env == env and r.status == "ok"), None)
                    if outcome is None or last is None:
                        continue
                    last.pruned = outcome.pruned
                    last.prune_pending = outcome.pending
                    last.prune_skipped = outcome.skipped
                    last.prune_error = outcome.error
                    last.shared_with = outcome.shared_with
                    if outcome.pruned:
                        _count(lambda m: m.inc_sink_pruned(_sink_kind(sink), datasource, outcome.pruned))
        finally:
            sink.close()

    # Skipped files from ETL become empty sync results
    for skipped_path in etl.skipped:
        results.append(SyncResult(
            file_path=skipped_path,
            env="unknown",
            status="skipped",
        ))
    # A file that could not be processed is a failed file, with its reason.
    for failed_path in etl.errors:
        results.append(SyncResult(
            file_path=failed_path,
            env="unknown",
            status="error",
            error=etl.failure(failed_path),
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
        **({"replaced": r.replaced, "replaced_by": r.replaced_by} if r.replaced else {}),
        **({"error": r.error} if r.error else {}),
        **({"prune_pending": r.prune_pending} if r.prune_pending is not None else {}),
        **({"prune_skipped": r.prune_skipped} if r.prune_skipped else {}),
        **({"prune_error": r.prune_error} if r.prune_error else {}),
        **({"key_space": r.key_space} if r.key_space else {}),
        **({"validation": r.validation, "schema": r.schema, "schema_source": r.schema_source}
           if r.validation else {}),
        **({"schema_note": r.schema_note} if r.schema_note else {}),
    }
