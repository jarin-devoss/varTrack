"""
app/ui/drift.py
────────────────
Does a datasource's live state match the desired state from Git?

The watcher asks this (CheckDatasource) whenever a poll sees a datasource
change, before healing. Its fingerprint changes for any write — including
the ones varTrack makes when a push is synced — so only a change that leaves
the sink different from Git, key by key, is drift.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from app.ui import ledger, views

logger = logging.getLogger(__name__)

IN_SYNC = "in_sync"
DRIFTED = "drifted"
SYNCING = "syncing"
UNKNOWN = "unknown"


def tenant_rules(tenant: str = "") -> Iterator[tuple[str, list[dict]]]:
    """(tenant, its rules) for *tenant*, or for every registered tenant: the
    rules a check and a heal work on, each built from config.cue, the tenant's
    bundle.json and its repositories' vartrack.json (rule_layers)."""
    from app.schema_registry.manager import get_schema_manager

    mgr = get_schema_manager()
    for tid in [tenant] if tenant else (mgr.list_tenant_ids() or ["default"]):
        try:
            mgr.get_or_clone(tid)
        except Exception:
            pass
        yield tid, mgr.get_all_rules(tid)


def watch_targets() -> list[tuple[str, dict]]:
    """The (tenant, rule) pairs the watcher keeps in sync: self_heal on, and
    not suspended (a suspended rule is neither healed nor compared)."""
    return [(tid, rule) for tid, rules in tenant_rules() for rule in rules
            if rule.get("self_heal") and not rule.get("suspend")]


def check_datasource(datasource: str, platform: str = "", tenant: str = "") -> tuple[str, list[str], str]:
    """Return (status, drifted environments, message) for every environment synced to *datasource*."""
    drifted: list[str] = []
    syncing: list[str] = []
    unknown: list[str] = []
    suspended: list[str] = []
    to_compare: list[tuple[dict, str, dict, str]] = []  # rule, tenant, record, "{rule}/{env}"

    for tid, all_rules in tenant_rules(tenant):
        rules = [
            r for r in all_rules
            if r.get("datasource") == datasource and (not platform or r.get("platform") == platform)
        ]
        # A suspended rule is not healed, so it is not compared either.
        suspended.extend(views.rule_name(r) for r in rules if r.get("suspend"))
        rules = [r for r in rules if not r.get("suspend")]
        if not rules:
            continue
        records = [r for r in ledger.list_targets(tid) if r.get("datasource") == datasource]
        tasks = ledger.list_tasks(tid, limit=200)
        for rule in rules:
            name = views.rule_name(rule)
            for record in records:
                where = f"{name}/{record['env']}"
                # A task records its environment when it finishes, so a running
                # one without an env may be writing to this environment too.
                # One abandoned by a dead worker no longer counts (ledger.is_active).
                if any(t.get("rule") == name and ledger.is_active(t) and t.get("env") in (None, record["env"]) for t in tasks):
                    syncing.append(where)
                    continue
                to_compare.append((rule, tid, record, where))

    # Read every environment at once: the watcher waits on this answer with a
    # single deadline, and one slow sink should cost its own wait, not the sum.
    compared = len(to_compare)
    if to_compare:
        with ThreadPoolExecutor(max_workers=min(8, compared), thread_name_prefix="check_ds") as pool:
            results = list(pool.map(lambda c: views.compare(c[0], c[1], c[2], fresh=True), to_compare))
        for (_, _, _, where), result in zip(to_compare, results):
            if result["error"]:
                unknown.append(f"{where}: {result['error']}")
            elif result["differs"]:
                drifted.append(where)

    if syncing:
        return SYNCING, drifted, f"sync running for {', '.join(syncing)}"
    if drifted:
        return DRIFTED, drifted, f"differs from Git: {', '.join(drifted)}"
    if unknown:
        return UNKNOWN, [], "; ".join(unknown)
    if not compared:
        if suspended:
            return UNKNOWN, [], f"suspended: {', '.join(suspended)}"
        return UNKNOWN, [], f"no synced environment recorded for {datasource}"
    return IN_SYNC, [], f"{compared} environment{'s' if compared != 1 else ''} match Git"
