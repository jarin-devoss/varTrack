from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class AmbiguousRule(ValueError):
    """Rules on several platforms write to the datasource, and none was named."""


def rule_config_for_datasource(tenant_id: str, datasource: str, platform: str = "") -> dict | None:
    """
    Return the rule_config dict for a (tenant, platform, datasource) rule.
    Used by CLI sync, which comes from no git platform: it names the rule's
    *platform* when rules on several platforms write to the datasource, and
    then it is needed. Raises AmbiguousRule when it is needed and missing.
    Returns None when no rule fits in the bundle.
    """
    try:
        from app.schema_registry.manager import get_schema_manager
        mgr = get_schema_manager()
        try:
            # A worker has no clone of the tenant's schema repo until something asks for one.
            mgr.get_or_clone(tenant_id)
        except Exception:
            logger.warning("schema repo of tenant %s could not be refreshed; using the rules already loaded", tenant_id)
        rules = [r for r in mgr.get_all_rules(tenant_id)
                 if r.get("datasource") == datasource and (not platform or r.get("platform") == platform)]
    except Exception:
        logger.exception("rule lookup failed tenant=%s datasource=%s", tenant_id, datasource)
        return None
    platforms = sorted({str(r.get("platform", "")) for r in rules})
    if len(platforms) > 1:
        raise AmbiguousRule(f"datasource {datasource!r} has rules on {', '.join(platforms)}: say which platform")
    return rules[0] if rules else None


def rule_from_bundle(platform: str, datasource: str, tenant_id: str) -> dict | None:
    try:
        from app.schema_registry.manager import get_schema_manager
        return get_schema_manager().resolve_rule(platform, datasource, tenant_id)
    except Exception:
        return None

def rule_from_any_tenant(platform: str, datasource: str) -> dict | None:
    """
    The rule for (platform, datasource) from whichever registered tenant has it
    (and may layer it), else config.cue's rule for them as its owner's
    (rule_layers).

    Used by the gRPC server, whose webhook requests carry no tenant. Nothing is
    cloned after a restart until something asks for it, so on a miss the
    registered tenants' schema repos are cloned and searched again — otherwise
    the first pushes after a restart are rejected as "no rule configured".
    """
    from app.schema_registry.manager import get_schema_manager
    mgr = get_schema_manager()
    rule = _loaded_rule(mgr, platform, datasource)
    if rule is None:
        for tid in mgr.list_tenant_ids():
            if not _reload_due(tid):
                continue
            try:
                mgr.get_or_clone(tid)
            except Exception:
                pass
        rule = _loaded_rule(mgr, platform, datasource)
    if rule is None:
        rule = _unclaimed_rule(mgr, platform, datasource)
    return rule


# Webhooks for a datasource no rule names (or a tenant whose clone keeps
# failing) would otherwise reach git on every request.
_RELOAD_EVERY_SECONDS = 30.0
_reloaded_at: dict[str, float] = {}
_reload_lock = threading.Lock()


def _reload_due(tenant_id: str) -> bool:
    now = time.monotonic()
    with _reload_lock:
        if now - _reloaded_at.get(tenant_id, -_RELOAD_EVERY_SECONDS) < _RELOAD_EVERY_SECONDS:
            return False
        _reloaded_at[tenant_id] = now
        return True


def _loaded_rule(mgr, platform: str, datasource: str) -> dict | None:
    # Entries are keyed "tenant_id:slug"; scan every tenant already loaded,
    # skipping a bundle.json rule its tenant may not layer (rule_layers).
    from app.pipeline.rule_layers import rule_for

    with mgr._lock:
        found = [(rule, rule.get("tenant_id") or entry.tenant_id)
                 for entry in mgr._entries.values() for rule in entry.rules
                 if rule.get("platform") == platform and rule.get("datasource") == datasource]
    claimed = _claimed(mgr)
    return next((r for r in (rule_for(platform, datasource, rule, tid, claimed) for rule, tid in found)
                 if r is not None), None)


def _claimed(mgr) -> set[tuple]:
    # The (tenant, platform, datasource) of the loaded bundle.json rules.
    with mgr._lock:
        return {(e.tenant_id, r.get("platform"), r.get("datasource"))
                for e in mgr._entries.values() for r in e.rules}


def _unclaimed_rule(mgr, platform: str, datasource: str) -> dict | None:
    # A config.cue rule no bundle.json names: its owner's — unless the owner's
    # bundle.json cannot be used, and then the owner has no rules at all.
    from app.pipeline.rule_layers import cue_rules, owner, rule_for

    cue = next((c for c in cue_rules() if c.get("platform") == platform and c.get("datasource") == datasource), None)
    if cue is None:
        return None
    tenant_id = owner(cue)
    bundle_error = getattr(mgr, "bundle_error", None)
    if callable(bundle_error) and bundle_error(tenant_id):
        return None
    return rule_for(platform, datasource, None, tenant_id, _claimed(mgr))


def rules_from_bundle_all(tenant_id: str | None = None) -> list[dict]:
    """
    Load all rules from the schema bundle for sync_all beat task.
    """
    try:
        from app.schema_registry.manager import get_schema_manager
        mgr = get_schema_manager()
        if tenant_id is not None:
            try:
                mgr.get_or_clone(tenant_id)
            except Exception:
                pass
            return mgr.get_all_rules(tenant_id) or []
        # Fan-out: collect rules from every registered tenant. A worker has
        # no clone of its own until something asks for one, so make sure each
        # tenant's schema repo is cloned (or refreshed) before reading it.
        all_rules: list[dict] = []
        tenant_ids = mgr.list_tenant_ids()
        for tid in tenant_ids:
            try:
                mgr.get_or_clone(tid)
            except Exception:
                pass
        # The config.cue rules no bundle.json names belong to their owner
        # (tenant "default" by default), whether or not it has a schema repo.
        from app.pipeline.rule_layers import DEFAULT_TENANT, owners
        tenant_ids = list(dict.fromkeys([*tenant_ids, DEFAULT_TENANT, *owners()]))
        for tid in tenant_ids:
            for rule in mgr.get_all_rules(tid):
                # Stamp tenant_id so sync_all_task dispatches to the right tenant.
                r = dict(rule)
                r.setdefault("tenant_id", tid)
                all_rules.append(r)
        return all_rules
    except Exception:
        return []
