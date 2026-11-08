"""
app/api/deps.py
────────────────
FastAPI dependencies shared by the routers that sit outside /v1/ui.

``require_viewer_or_grant``  the read routes the UI and service callers share
                             (task status, the tenant's schema list)
``call_identity``            platform / tenant / datasource of a webhook-style
                             call: from its grant when it has one, from the
                             request headers only while internal authentication
                             is off
``route_push``               the platform, tenant and rule a pushed payload is
                             for; shared by the webhook and its dry run, so a
                             dry run previews the sync the push would run
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

from app.api import tokens


def request_grant(request: Request) -> Optional[tokens.Grant]:
    """The verified grant of this request, if the gate set one."""
    return getattr(request.state, "grant", None)


def require_viewer_or_grant(request: Request) -> None:
    """Allow a verified grant, or a signed-in UI session.

    With internal authentication off and UI sign-in off, these routes stay open
    as they always were (a development setup). With either on, an anonymous
    caller is turned away: sign-in being off is no reason to let anyone read the
    results of other people's syncs once service authentication is on.
    """
    if request_grant(request) is not None:
        return
    from app.api.routers.ui import _viewer
    try:
        viewer = _viewer(request)
    except HTTPException as exc:
        # UI sign-in is on and there is no session. When grants are accepted too, say so: "sign in required"
        # alone would tell a service that a signed grant is no use here.
        if tokens.enforced():
            raise HTTPException(status_code=401,
                                detail="a signed grant or a signed-in session is required") from exc
        raise
    if tokens.enforced() and not viewer.authenticated:
        raise HTTPException(status_code=401, detail="a signed grant or a signed-in session is required")


def tenant_in_scope(request: Request, tenant_id: str) -> None:
    """The tenant is a tenant name, one config.cue declares (any name while it declares none), and
    a grant scoped to one tenant cannot act on another."""
    from app import tenants
    try:
        tenants.check_name(tenant_id)
    except tenants.InvalidTenant as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not tenants.is_declared(tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id!r} is not declared")
    grant = request_grant(request)
    if grant is not None and grant.tenant and grant.tenant != tenant_id:
        raise HTTPException(status_code=403, detail="the grant was issued for another tenant")


@dataclass(frozen=True)
class CallIdentity:
    platform: str
    tenant: str            # "" when the call names none
    grant: Optional[tokens.Grant]


def call_identity(request: Request, datasource: str, sniffed_platform: str) -> CallIdentity:
    """Who the call is for.

    A grant is authoritative: its platform and tenant are the ones the gateway
    authorised, and a call for another datasource than the one it was issued for
    is refused. Only when internal authentication is off do the request headers
    stand in for it.
    """
    grant = request_grant(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if grant is None:
        return CallIdentity(sniffed_platform, headers.get("x-tenant-id", ""), None)
    if grant.datasource and grant.datasource != datasource:
        raise HTTPException(status_code=403, detail="the grant was issued for another datasource")
    return CallIdentity(
        platform=grant.platform or sniffed_platform,
        tenant=grant.tenant or headers.get("x-tenant-id", ""),
        grant=grant,
    )


def sniff_platform(headers: dict[str, str]) -> str:
    """The git platform a webhook comes from, by its event header (lower-cased names)."""
    # Gitea also sends X-GitHub-Event (for compatibility), so its own header is looked at first.
    if "x-gitea-event" in headers:
        return "gitea"
    if "x-github-event" in headers:
        return "github"
    if "x-gitlab-event" in headers:
        return "gitlab"
    if "x-bitbucket-event" in headers:
        return "bitbucket"
    return headers.get("x-platform", "unknown")


@dataclass(frozen=True)
class PushRoute:
    platform: str
    tenant_id: str
    rule_config: dict


def route_push(request: Request, datasource: str, raw_payload: str, *, what: str = "webhook") -> PushRoute:
    """Which platform, tenant and rule a pushed payload is for: what a real sync and its dry run share.

    The pushed repository decides the tenant: the one whose repositories it is in. A tenant in the
    grant (or, with internal authentication off, a header) is checked against that, never used in its
    place, and a payload's repository owner is never one. The rule is the one the tenant's layers give
    (config.cue's, with its bundle.json laid over it), and the pushed repository has to be one that
    rule covers. A push no declared tenant owns is refused, before anything is queued. Raises
    HTTPException 403 for each of those refusals; *what* names the call in the log.
    """
    import json as _json
    import logging

    from app import tenants
    from app.pipeline.repo_scope import RepositoryNotAllowed, check_repository, pushed_repositories

    log = logging.getLogger(__name__)
    raw_headers = {k.lower(): v for k, v in request.headers.items()}
    identity = call_identity(request, datasource, sniff_platform(raw_headers))
    platform = identity.platform
    try:
        payload = _json.loads(raw_payload)
        pushed = pushed_repositories(payload)
    except ValueError:
        payload, pushed = None, []
    claimed = identity.tenant or None
    owners = tenants.tenants_of_repositories(pushed)
    mgr = getattr(request.app.state, "schema_manager", None)
    resolve_as = owners[0] if len(owners) == 1 else claimed
    rule_config = (
        mgr.resolve_rule(platform, datasource, resolve_as)
        if mgr and resolve_as
        else (mgr.resolve_rule(platform, datasource) if mgr else {})
    )
    rule_config = rule_config or {}

    # The repository the payload names has to be one the rule covers (and not one it excludes): the
    # payload is the sender's claim, the rule the operator's decision.
    if rule_config and payload is not None:
        try:
            check_repository(rule_config, payload)
        except RepositoryNotAllowed as exc:
            log.warning("%s refused datasource=%s platform=%s: %s", what, datasource, platform, exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    tenant_id = tenants.tenant_of_push(rule_config, pushed)
    if tenant_id is None:
        log.warning("%s refused datasource=%s platform=%s: no declared tenant owns %s",
                    what, datasource, platform, ", ".join(pushed) or "the repository")
        raise HTTPException(
            status_code=403,
            detail=f"no declared tenant owns {', '.join(pushed) or 'the repository this push names'}",
        )
    if claimed and claimed != tenant_id:
        raise HTTPException(status_code=403, detail="the grant was issued for another tenant")
    return PushRoute(platform=platform, tenant_id=tenant_id, rule_config=rule_config)
