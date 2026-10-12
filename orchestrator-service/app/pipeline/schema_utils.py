from __future__ import annotations

import threading
import time

def rule_config_for_datasource(tenant_id: str, datasource: str) -> dict | None:
    """
    Return the rule_config dict for a (tenant, datasource) pair.
    Used by CLI sync which has no git platform context.
    Returns None when the datasource is not found in the bundle.
    """
    try:
        from app.schema_registry.manager import get_schema_manager
        mgr = get_schema_manager(tenant_id)
        if mgr is None:
            return None
        return mgr.resolve_rule_by_datasource(datasource, tenant_id)
    except Exception:
        return None


def rule_from_bundle(platform: str, datasource: str, tenant_id: str) -> dict | None:
    try:
        from app.schema_registry.manager import get_schema_manager
        return get_schema_manager().resolve_rule(platform, datasource, tenant_id)
    except Exception:
        return None

def rule_from_any_tenant(platform: str, datasource: str) -> dict | None:
    """
    The rule for (platform, datasource) from whichever registered tenant has it.

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
    # Entries are keyed "tenant_id:slug"; scan every tenant already loaded.
    with mgr._lock:
        for entry in mgr._entries.values():
            for rule in entry.rules:
                if rule.get("platform") == platform and rule.get("datasource") == datasource:
                    return rule
    return None


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
        for tid in mgr.list_tenant_ids():
            try:
                mgr.get_or_clone(tid)
            except Exception:
                pass
            for rule in mgr.get_all_rules(tid):
                # Stamp tenant_id so sync_all_task dispatches to the right tenant.
                r = dict(rule)
                r.setdefault("tenant_id", tid)
                all_rules.append(r)
        return all_rules
    except Exception:
        return []
