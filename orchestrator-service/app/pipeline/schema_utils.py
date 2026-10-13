from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def rule_config_for_datasource(tenant_id: str, datasource: str) -> dict | None:
    """
    Return the rule_config dict for a (tenant, datasource) pair.
    Used by CLI sync which has no git platform context: when rules on several
    platforms sync to the datasource, the first one in the bundle is used.
    Returns None when the datasource is not found in the bundle.
    """
    try:
        from app.schema_registry.manager import get_schema_manager
        mgr = get_schema_manager()
        try:
            # A worker has no clone of the tenant's schema repo until something asks for one.
            mgr.get_or_clone(tenant_id)
        except Exception:
            logger.warning("schema repo of tenant %s could not be refreshed; using the rules already loaded", tenant_id)
        return next((r for r in mgr.get_all_rules(tenant_id) if r.get("datasource") == datasource), None)
    except Exception:
        logger.exception("rule lookup failed tenant=%s datasource=%s", tenant_id, datasource)
        return None


def rule_from_bundle(platform: str, datasource: str, tenant_id: str) -> dict | None:
    try:
        from app.schema_registry.manager import get_schema_manager
        return get_schema_manager().resolve_rule(platform, datasource, tenant_id)
    except Exception:
        return None

def rules_from_bundle_all(tenant_id: str | None = None) -> list[dict]:
    """
    Load all rules from the schema bundle for sync_all beat task.
    """
    try:
        from app.schema_registry.manager import get_schema_manager
        mgr = get_schema_manager()
        if tenant_id is not None:
            return mgr.get_all_rules(tenant_id) or []
        # Fan-out: collect rules from every registered tenant.
        all_rules: list[dict] = []
        for tid in mgr.list_tenant_ids():
            for rule in mgr.get_all_rules(tid):
                # Stamp tenant_id so sync_all_task dispatches to the right tenant.
                r = dict(rule)
                r.setdefault("tenant_id", tid)
                all_rules.append(r)
        return all_rules
    except Exception:
        return []
