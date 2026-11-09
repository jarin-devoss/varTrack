"""
app/api/middlewares/auth.py
────────────────────────────
One gate for the whole HTTP API.

A route is authenticated by default: it needs a signed grant (app/api/tokens.py)
in ``Authorization: Bearer <grant>`` unless it is listed below as one of the few
surfaces that answer for themselves. A route added later is therefore closed
until someone opens it on purpose, never open until someone remembers to close it.

    public    /v1/health*, /v1/auth/*          probes and the sign-in flow
    session   /v1/ui/*                         the web UI's own session check
    either    GET /v1/tasks/*, GET /v1/tenants/*/schemas
              a grant, or a signed-in UI session (decided by the route's
              dependency, app/api/deps.py)
    grant     everything else                  webhooks, dry runs, schema warm-up,
                                               the API docs

The gate is a no-op while internal authentication is off (development; see
app/api/tokens.py). When it passes a request it leaves the verified grant on
``request.state.grant``; the handlers read the tenant, platform and datasource
from it, never from headers or the payload.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api import tokens

logger = logging.getLogger(__name__)

PUBLIC  = "public"
SESSION = "session"
EITHER  = "either"
GRANT   = "grant"

_PUBLIC_EXACT   = {"/v1/health", "/v1/health/liveness", "/v1/health/readiness"}
_PUBLIC_PREFIX  = ("/v1/auth/",)
_SESSION_PREFIX = ("/v1/ui/",)


def classify(method: str, path: str) -> str:
    """Which kind of credential a route takes. *path* has no trailing slash."""
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX):
        return PUBLIC
    if path.startswith(_SESSION_PREFIX):
        return SESSION
    if method in ("GET", "HEAD"):
        if path.startswith("/v1/tasks/"):
            return EITHER
        parts = path.strip("/").split("/")          # v1 / tenants / {id} / schemas
        if len(parts) == 4 and parts[:2] == ["v1", "tenants"] and parts[3] == "schemas":
            return EITHER
    return GRANT


def _unauthorized(detail: str) -> Response:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


class InternalAuthMiddleware(BaseHTTPMiddleware):
    """Require a signed grant on every route that is not public or session-checked."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not tokens.enforced():
            return await call_next(request)

        kind = classify(request.method, request.url.path)
        if kind in (PUBLIC, SESSION):
            return await call_next(request)

        token = tokens.bearer(request.headers.get("authorization"))
        if token:
            try:
                request.state.grant = tokens.verify(token, tokens.AUDIENCE_SYNC)
            except tokens.GrantError as exc:
                if kind == GRANT:
                    logger.warning("rejected %s %s: %s", request.method, request.url.path, exc)
                    return _unauthorized(f"invalid grant: {exc}")
                # "either": a bad grant is no grant; the session check decides
                request.state.grant = None
            return await call_next(request)

        if kind == EITHER:
            request.state.grant = None
            return await call_next(request)
        return _unauthorized("a signed grant is required: Authorization: Bearer <grant>")
