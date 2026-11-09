"""
app/tenants.py
──────────────
What a tenant is, and where its name may come from.

A tenant is a team the operator declared in config.cue's ``tenants``: a name, the
repositories whose pushes are its own, a schema repository, the destinations it may write and
the places its presets may come from. A request that carries a name is asking for a tenant,
not creating or choosing one: whatever reads a name from a header, a body or a URL checks its
shape here first, and what the caller may do for that tenant is decided elsewhere.

The tenant of a push is the one whose repositories the pushed repository is in; the tenant of
a schema push is the one whose schema repository it is. Neither comes from what the request
says about itself. With no tenants declared there is one implicit tenant, "default", and
everything is as it was.

The shape is letters, digits, "-" and "_", starting with a letter or digit, at most 63
characters. The gateway and the vt CLI check the same shape, so a name that is not one never
reaches a query string, a Redis key or a path.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")


class InvalidTenant(ValueError):
    """A tenant name that is not the right shape."""


def valid_name(name: object) -> bool:
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def check_name(name: str) -> str:
    """*name*, or InvalidTenant. The message never repeats more than a short head of it."""
    if not valid_name(name):
        shown = name if isinstance(name, str) and len(name) <= 40 else f"{str(name)[:40]}…"
        raise InvalidTenant(
            f"tenant {shown!r} is not a tenant name: letters, digits, '-' and '_' only, up to 63 characters")
    return name


# ── The declared tenants ──────────────────────────────────────────────────────

def declared() -> list[dict]:
    """The tenants config.cue declares, as the bundle has them. Empty: one implicit tenant."""
    from app.pipeline.datasource_settings import _bundle
    return [t for t in (_bundle().get("tenants") or []) if isinstance(t, dict) and t.get("name")]


def declared_names() -> list[str]:
    return [t["name"] for t in declared()]


def is_declared(name: str) -> bool:
    """Whether *name* is a tenant: any valid name while none are declared, else a declared one."""
    names = declared_names()
    return valid_name(name) and (not names or name in names)


def _covers(patterns: Iterable[str], repo: str) -> bool:
    return any(fnmatch.fnmatchcase(repo.lower(), p.lower()) for p in patterns)


def tenants_of_repositories(repositories: list[str]) -> list[str]:
    """The declared tenants whose repositories include every name in *repositories* (the
    spellings a payload gives its repository must all agree)."""
    if not repositories:
        return []
    return [t["name"] for t in declared()
            if all(_covers(t.get("repositories") or [], repo) for repo in repositories)]


def tenant_of_push(rule: dict, repositories: list[str]) -> Optional[str]:
    """The tenant a push belongs to, or None when no tenant is entitled to it.

    The pushed repositories decide, never a header or a payload field: the tenant whose
    ``repositories`` cover them, narrowed to the tenants the rule names when it names any (a
    tenant the rule leaves out does not get the push). A repository no tenant covers goes to
    the rule's only tenant when it names exactly one, else it belongs to nobody. With no tenants declared the push is the rule's
    tenant's ("default" when it has none)."""
    names = declared_names()
    if not names:
        return rule.get("tenant_id") or "default"
    allowed = [n for n in (rule.get("tenants") or []) if n in names]
    owners = tenants_of_repositories(repositories)
    if not owners:
        # no tenant claims the repository: the rule's only tenant takes it, else nobody does
        return allowed[0] if len(allowed) == 1 else None
    if allowed:
        owners = [n for n in owners if n in allowed]
    if len(owners) == 1:
        return owners[0]
    if len(owners) > 1:
        logger.warning("push from %s matches several tenants (%s)", ", ".join(repositories), ", ".join(owners))
    return None


def tenant_of_schema_push(repository: str) -> Optional[str]:
    """The tenant whose registered schema repository is *repository*, else None."""
    from app.schema_registry.manager import get_schema_manager
    return get_schema_manager().tenant_for_repo(repository)


def describe(tenant_name: str) -> dict[str, Any]:
    """The declared tenant called *tenant_name*, or {}."""
    return next((t for t in declared() if t["name"] == tenant_name), {})


class DestinationRefused(PermissionError):
    """A sync would write somewhere that is not the tenant's."""


def may_write(tenant_name: str, datasource: str, destination: str) -> None:
    """Raise DestinationRefused unless *tenant_name* may write *destination* on *datasource*.

    Where no declared tenant lists destinations for the datasource, every tenant may write it
    (a single-tenant install, as before). Once any does, a tenant writes only what its own
    ``destinations`` for that datasource cover, and a tenant with none there writes nothing."""
    from app.pipeline.destinations import describe, matches

    everyone = declared()
    if not any(describe(t, datasource) for t in everyone):
        return
    mine = describe(next((t for t in everyone if t["name"] == tenant_name), {}), datasource)
    if matches(destination, mine):
        return
    raise DestinationRefused(
        f"destination {destination!r} is not one of tenant {tenant_name}'s on {datasource} "
        f"({', '.join(mine) if mine else 'it has none there'})")
