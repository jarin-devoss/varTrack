#!/usr/bin/env python3
"""
e2e/init/grant.py
─────────────────
Signed grants for the demo's own calls to the orchestrator.

With internal authentication on (INTERNAL_AUTH_SECRET) the orchestrator accepts a call only when it
carries ``Authorization: Bearer <grant>``: a compact HS256 token the gateway, the watcher and operators
mint, and the orchestrator verifies (orchestrator-service/app/api/tokens.py). The demo's test runner and
demo.sh play the gateway's part, so they mint grants the same way.

Standard library only: e2e/init/requirements.txt is hash-pinned and does not change for this.

    python3 grant.py mint --audience sync --subject e2e-runner --datasource mongo
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time

ISSUER = "vartrack"
AUDIENCES = ("sync", "watch", "admin", "authz")

# A bearer value, wherever it turns up in text that is about to be printed.
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(secret: str, signing_input: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest())


def mint(
    audience: str = "sync",
    *,
    secret: str,
    subject: str = "",
    tenant: str = "",
    platform: str = "",
    datasource: str = "",
    ttl: int = 60,
    now: float | None = None,
) -> str:
    """A grant with the claims of app.api.tokens.mint: ``iss``, ``aud``, ``iat``/``nbf``/``exp`` and the
    ones that are set, signed HS256 over ``header.payload``. *ttl* may be negative with *now* in the past to
    make an expired one for a test; the orchestrator's own lifetime limits apply to a valid one."""
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {AUDIENCES}")
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": ISSUER, "aud": audience, "iat": issued, "nbf": issued, "exp": issued + ttl,
        "sub": subject, "tenant": tenant, "platform": platform, "datasource": datasource,
    }
    claims = {k: v for k, v in claims.items() if v not in ("", None)}
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}"
    return f"{signing_input}.{_sign(secret, signing_input)}"


def redact(text: str) -> str:
    """*text* with every bearer token in it hidden, and the demo secret too, so a log line or a failure
    message can carry a request without carrying the credential."""
    text = _BEARER.sub(r"\1******", text)
    secret = os.environ.get("INTERNAL_AUTH_SECRET", "")
    return text.replace(secret, "******") if secret else text


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="grant.py", description="Mint a signed grant for the demo.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="print a signed grant")
    m.add_argument("--audience", default="sync", choices=AUDIENCES)
    m.add_argument("--subject", default="e2e-demo")
    m.add_argument("--tenant", default="")
    m.add_argument("--platform", default="gitea")
    m.add_argument("--datasource", default="")
    m.add_argument("--ttl", type=int, default=60, help="lifetime in seconds")
    args = parser.parse_args(argv)
    secret = os.environ.get("INTERNAL_AUTH_SECRET", "")
    if not secret:
        print("INTERNAL_AUTH_SECRET is not set", file=sys.stderr)
        return 2
    print(mint(args.audience, secret=secret, subject=args.subject, tenant=args.tenant,
               platform=args.platform, datasource=args.datasource, ttl=args.ttl))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
