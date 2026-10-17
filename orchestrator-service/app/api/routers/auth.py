"""
app/api/routers/auth.py
────────────────────────
Single sign-on for the web UI with the Git platforms, mounted under /v1/auth.

  GET  /auth/session                     is sign-in on, which platforms, who is signed in (never 401)
  GET  /auth/login?platform=&return_to=  start: redirect to that platform's consent page
  GET  /auth/callback                    a platform redirects back here with a code
  POST /auth/logout                      end the session (every connected platform)

Signing in with a platform while already signed in with another connects it
to the same session, so rules from both are visible. The flow is the OAuth2
authorization code flow with PKCE; the state is single-use, expires in five
minutes, names the platform, and is bound to the browser that started the
login by a cookie. See app/ui/sessions.py for configuration.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.ui import access, scm, sessions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

MISCONFIGURED = "sign-in is misconfigured; the orchestrator's log says why"


def _config_or_none() -> sessions.AuthConfig | None:
    try:
        return sessions.config()
    except sessions.AuthConfigError as exc:
        logger.error("ui auth: %s", exc)
        return None


def _login_error(code: str) -> RedirectResponse:
    resp = RedirectResponse(f"/login?{urlencode({'error': code})}", status_code=303)
    resp.delete_cookie(sessions.STATE_COOKIE, path="/")
    return resp


def _rules_per_platform(request: Request) -> dict[str, int]:
    """How many of the tenant's rules each platform holds — for "connect GitHub to see 3 more"."""
    try:
        from app.api.routers.ui import _rules, _tenant
        counts: dict[str, int] = {}
        for rule in _rules(_tenant(request)):
            counts[rule.get("platform", "")] = counts.get(rule.get("platform", ""), 0) + 1
        return counts
    except Exception:
        logger.debug("ui auth: counting rules failed", exc_info=True)
        return {}


@router.get("/session")
def get_session(request: Request) -> dict[str, Any]:
    try:
        cfg = sessions.config()
    except sessions.AuthConfigError as exc:
        # Misconfigured sign-in must not fall back to an open UI. What is wrong
        # (settings, file and Vault paths) goes to the log, not to visitors.
        logger.error("ui auth: %s", exc)
        return {"enabled": True, "authenticated": False, "platforms": [], "error": MISCONFIGURED}
    if cfg is None:
        return {"enabled": False, "authenticated": False, "platforms": []}

    session = sessions.get_session(cfg, request.cookies.get(sessions.SESSION_COOKIE))
    if not session:
        # Signed out: only what the sign-in buttons need — no addresses, no rule counts.
        return {"enabled": True, "authenticated": False,
                "platforms": [{"platform": name, "provider": provider.kind} for name, provider in cfg.platforms.items()]}
    identities = session["identities"]
    counts = _rules_per_platform(request)
    view: dict[str, Any] = {
        "enabled": True,
        "authenticated": True,
        "platforms": [
            {
                "platform": name,
                "provider": provider.kind,
                "providerUrl": provider.public_url,
                "connected": name in identities,
                **({"user": identities[name]["user"]} if name in identities else {}),
                "rules": counts.get(name, 0),
            }
            for name, provider in cfg.platforms.items()
        ],
    }
    view.update(user=identities[session["primary"]]["user"], primary=session["primary"],
                startedAt=session.get("startedAt"),
                admin=any(cfg.is_admin(p, i["user"]["login"]) for p, i in identities.items()))
    return view


@router.get("/login")
def login(platform: str = "", return_to: str = "/") -> Response:
    cfg = _config_or_none()
    if cfg is None:
        return RedirectResponse("/", status_code=303)
    if not platform and len(cfg.platforms) == 1:
        platform = next(iter(cfg.platforms))
    provider = cfg.platforms.get(platform)
    if provider is None:
        return _login_error("platform")
    state, challenge = sessions.start_login(platform, sessions.safe_return_path(return_to))
    resp = RedirectResponse(
        provider.authorize_url(state=state, redirect_uri=cfg.redirect_uri, code_challenge=challenge),
        status_code=303,
    )
    # Binds the login to this browser: a callback carrying someone else's state is refused.
    resp.set_cookie(sessions.STATE_COOKIE, state, max_age=sessions.LOGIN_STATE_TTL_SECONDS, path="/",
                    httponly=True, samesite="lax", secure=cfg.secure_cookies)
    return resp


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    cfg = _config_or_none()
    if cfg is None:
        return RedirectResponse("/", status_code=303)
    if error:
        # access_denied: the user said no on the consent page.
        logger.info("ui auth: platform returned error=%s", error)
        return _login_error("denied" if error == "access_denied" else "provider")
    if not state or request.cookies.get(sessions.STATE_COOKIE) != state:
        return _login_error("state")
    login_state = sessions.finish_login(state)
    provider = cfg.platforms.get((login_state or {}).get("platform", ""))
    if login_state is None or provider is None:
        return _login_error("expired")
    platform = login_state["platform"]
    try:
        token = provider.exchange(code=code, redirect_uri=cfg.redirect_uri, code_verifier=login_state["verifier"])
        user = provider.user(token["access_token"])
    except scm.ScmError as exc:
        logger.warning("ui auth: sign-in with %s failed: %s", platform, exc)
        return _login_error("provider")

    resp = RedirectResponse(sessions.safe_return_path(login_state.get("returnTo")), status_code=303)
    resp.delete_cookie(sessions.STATE_COOKIE, path="/")
    session_id = request.cookies.get(sessions.SESSION_COOKIE)
    session = sessions.get_session(cfg, session_id)
    if session and session_id:
        # Already signed in: connect this platform's account to the same session.
        sessions.connect_identity(cfg, session_id, session, platform=platform, user=user, token=token)
        access.forget(session_id)
        logger.info("ui auth: %s connected %s as %s", session["identities"][session["primary"]]["user"]["login"],
                    platform, user.login)
        return resp
    session_id = sessions.create_session(cfg, platform=platform, user=user, token=token)
    logger.info("ui auth: %s signed in with %s", user.login, platform)
    resp.set_cookie(sessions.SESSION_COOKIE, session_id, max_age=cfg.session_ttl, path="/",
                    httponly=True, samesite="lax", secure=cfg.secure_cookies)
    return resp


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    session_id = request.cookies.get(sessions.SESSION_COOKIE)
    sessions.delete_session(session_id)
    access.forget(session_id)
    resp = Response(status_code=204)
    resp.delete_cookie(sessions.SESSION_COOKIE, path="/")
    return resp
