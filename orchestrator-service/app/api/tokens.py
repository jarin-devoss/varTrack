"""
app/api/tokens.py
──────────────────
The credential every call to the orchestrator carries.

The orchestrator does not trust where a call comes from, only what it carries:
a *grant*, a compact HS256-signed token (``header.payload.signature``, base64url
— the usual JWT layout) that names who is asking and what the call may touch.

    aud         which API the grant is for: "sync", "watch", "admin" or "authz"
    sub         the caller ("login@platform" for a CLI user, a service name otherwise)
    tenant      the tenant the call is scoped to (optional)
    platform    the Git platform the call was authorised on (optional)
    datasource  the datasource the call was authorised for (optional)
    iat/nbf/exp issue, not-before and expiry times (all required)

The gateway mints "sync" grants after it has checked the webhook signature or
the caller's Git permissions; the watcher mints "watch" grants for its own
calls; an operator mints an "admin" grant for the metrics scrape
(``python -m app.api.tokens mint --audience admin ...``). The orchestrator mints
"authz" grants itself, for the questions it asks the gateway's permission engine
(app/authz_client.py). The Go side is libs/go/grants.

A signature, audience and lifetime are checked on every call; the claims then
bound what the call may do (see app/api/middlewares/auth.py, the gRPC
interceptor in app/grpc_server/auth_interceptor.py, and the handlers).

Enforcement
───────────
Grants are enforced whenever INTERNAL_AUTH_SECRET is set. Without it the
orchestrator runs open — the development mode — and says so at startup; in
production it refuses to start unless INTERNAL_AUTH_INSECURE=true says the
operator really means it.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

AUDIENCE_SYNC  = "sync"
AUDIENCE_WATCH = "watch"
AUDIENCE_ADMIN = "admin"
AUDIENCE_AUTHZ = "authz"
AUDIENCES = (AUDIENCE_SYNC, AUDIENCE_WATCH, AUDIENCE_ADMIN, AUDIENCE_AUTHZ)

ISSUER = "vartrack"
MIN_SECRET_LENGTH = 32
LEEWAY_SECONDS = 30

# The longest a grant may live, from issue to expiry. Calls are authorised one
# at a time, so the per-call audiences are short; the admin grant is a standing
# credential for a scraper.
MAX_LIFETIME_SECONDS = {
    AUDIENCE_SYNC:  600,
    AUDIENCE_WATCH: 600,
    AUDIENCE_AUTHZ: 600,
    AUDIENCE_ADMIN: 30 * 24 * 3600,
}


class GrantError(Exception):
    """A grant that must not be honoured. The message is safe to return to the
    caller: it never includes the token or the key."""


@dataclass(frozen=True)
class Grant:
    audience: str
    subject: str
    tenant: str = ""
    platform: str = ""
    datasource: str = ""
    issued_at: int = 0
    expires_at: int = 0


# The grant of the call being served, set by the gRPC interceptor for the
# duration of one handler so the servicers read who is asking from here and
# never from client-set metadata.
_current_grant: contextvars.ContextVar[Optional[Grant]] = contextvars.ContextVar(
    "vartrack_current_grant", default=None
)


def current_grant() -> Optional[Grant]:
    return _current_grant.get()


def set_current_grant(grant: Optional[Grant]) -> contextvars.Token:
    return _current_grant.set(grant)


def reset_current_grant(token: contextvars.Token) -> None:
    _current_grant.reset(token)


# ── keys and enforcement ─────────────────────────────────────────────────────

def _settings():
    from app.config import settings
    return settings


def key_for(audience: str) -> str:
    """The secret that signs and verifies grants for *audience*; the watcher's
    and the admin listener's may be separate keys, and fall back to the main one."""
    s = _settings()
    if audience == AUDIENCE_WATCH:
        return s.INTERNAL_AUTH_WATCH_SECRET or s.INTERNAL_AUTH_SECRET
    if audience == AUDIENCE_ADMIN:
        return s.INTERNAL_AUTH_ADMIN_SECRET or s.INTERNAL_AUTH_SECRET
    if audience == AUDIENCE_AUTHZ:
        return s.INTERNAL_AUTH_AUTHZ_SECRET or s.INTERNAL_AUTH_SECRET
    return s.INTERNAL_AUTH_SECRET


def enforced() -> bool:
    """True when calls must carry a grant."""
    return bool(_settings().INTERNAL_AUTH_SECRET)


def check_startup(service: str) -> None:
    """Called when a listener starts. Rejects an unusable configuration and says
    plainly when the orchestrator is running open."""
    s = _settings()
    for name in ("INTERNAL_AUTH_SECRET", "INTERNAL_AUTH_WATCH_SECRET", "INTERNAL_AUTH_ADMIN_SECRET",
                 "INTERNAL_AUTH_AUTHZ_SECRET"):
        value = getattr(s, name)
        if value and len(value) < MIN_SECRET_LENGTH:
            raise RuntimeError(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
    if enforced():
        logger.info("%s: internal authentication is on — calls must carry a signed grant", service)
        return
    if s.APP_ENV == "production" and not s.INTERNAL_AUTH_INSECURE:
        raise RuntimeError(
            "production mode requires INTERNAL_AUTH_SECRET (or INTERNAL_AUTH_SECRET_FILE): the "
            "orchestrator would otherwise accept calls from anyone who can reach it. "
            "Set INTERNAL_AUTH_INSECURE=true only to run open on purpose."
        )
    logger.warning(
        "%s: internal authentication is OFF — every call is accepted without a grant. "
        "Set INTERNAL_AUTH_SECRET before exposing this orchestrator beyond a development machine.",
        service,
    )


# ── encoding ─────────────────────────────────────────────────────────────────

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(key: str, signing_input: str) -> str:
    return _b64e(hmac.new(key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest())


def mint(
    audience: str,
    *,
    secret: str,
    subject: str = "",
    tenant: str = "",
    platform: str = "",
    datasource: str = "",
    ttl_seconds: int = 60,
    now: Optional[float] = None,
) -> str:
    """Sign a grant. The orchestrator itself only verifies; this is for the
    operator's admin token, for tests, and for callers written in Python."""
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {AUDIENCES}")
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"the signing secret must be at least {MIN_SECRET_LENGTH} characters")
    if not 0 < ttl_seconds <= MAX_LIFETIME_SECONDS[audience]:
        raise ValueError(f"a {audience} grant may live at most {MAX_LIFETIME_SECONDS[audience]} seconds")
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": ISSUER, "aud": audience, "iat": issued, "nbf": issued, "exp": issued + ttl_seconds,
        "sub": subject, "tenant": tenant, "platform": platform, "datasource": datasource,
    }
    claims = {k: v for k, v in claims.items() if v not in ("", None)}
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}"
    return f"{signing_input}.{_signature(secret, signing_input)}"


def verify(
    token: str,
    audience: str,
    *,
    keys: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> Grant:
    """Check *token* and return its claims, or raise GrantError.

    The signature is checked first, with a constant-time comparison, and only a
    token the right key signed has its claims read. The algorithm is fixed:
    whatever the header says, only HS256 is accepted.

    *keys* maps an audience to its secret; it defaults to the configured keys.
    """
    secret = (keys or {}).get(audience) if keys is not None else key_for(audience)
    if not secret:
        raise GrantError("no key is configured for this audience")
    parts = (token or "").split(".")
    if len(parts) != 3 or not all(parts):
        raise GrantError("malformed token")
    header_b64, body_b64, sig_b64 = parts
    try:
        header = json.loads(_b64d(header_b64))
    except Exception:
        raise GrantError("malformed token") from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise GrantError("unsupported token algorithm")
    expected = _signature(secret, f"{header_b64}.{body_b64}")
    if not hmac.compare_digest(expected.encode("ascii"), sig_b64.encode("ascii", errors="replace")):
        raise GrantError("invalid signature")
    try:
        claims = json.loads(_b64d(body_b64))
    except Exception:
        raise GrantError("malformed token") from None
    if not isinstance(claims, dict):
        raise GrantError("malformed token")

    if claims.get("iss") != ISSUER:
        raise GrantError("wrong issuer")
    if claims.get("aud") != audience:
        raise GrantError("wrong audience")
    try:
        iat, nbf, exp = int(claims["iat"]), int(claims["nbf"]), int(claims["exp"])
    except (KeyError, TypeError, ValueError):
        raise GrantError("the token must carry iat, nbf and exp") from None
    current = time.time() if now is None else now
    if current < nbf - LEEWAY_SECONDS or current < iat - LEEWAY_SECONDS:
        raise GrantError("token is not valid yet")
    if current > exp + LEEWAY_SECONDS:
        raise GrantError("token has expired")
    if exp - iat > MAX_LIFETIME_SECONDS[audience]:
        raise GrantError("token lifetime is longer than this audience allows")

    def text(name: str) -> str:
        value = claims.get(name, "")
        return value if isinstance(value, str) else ""

    return Grant(
        audience=audience,
        subject=text("sub"),
        tenant=text("tenant"),
        platform=text("platform"),
        datasource=text("datasource"),
        issued_at=iat,
        expires_at=exp,
    )


def bearer(authorization: Optional[str]) -> str:
    """The token in an ``Authorization: Bearer <token>`` value, or ""."""
    if not authorization:
        return ""
    scheme, _, rest = authorization.strip().partition(" ")
    return rest.strip() if scheme.lower() == "bearer" else ""


# ── command line: mint an admin grant ────────────────────────────────────────

def _main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m app.api.tokens", description="Mint a signed grant.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="print a signed grant")
    m.add_argument("--audience", required=True, choices=AUDIENCES)
    m.add_argument("--subject", default="")
    m.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    args = parser.parse_args(argv)
    secret = key_for(args.audience)
    if not secret:
        print("no signing secret is configured (INTERNAL_AUTH_SECRET)", file=sys.stderr)
        return 2
    print(mint(args.audience, secret=secret, subject=args.subject, ttl_seconds=args.ttl))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
