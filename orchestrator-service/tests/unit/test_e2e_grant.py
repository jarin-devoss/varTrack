"""
The demo's grant helper (e2e/init/grant.py) mints what the orchestrator verifies.

It is a second implementation of the grant format, written with the standard library because the demo
image's requirements are hash-pinned. This is the one place it could drift from app/api/tokens.py, so
every property the demo's Suite 18 relies on is checked here against the real verifier.
"""
from __future__ import annotations

import importlib.util
import pathlib
import time

import pytest

from app.api import tokens

SECRET = "demo-secret-demo-secret-demo-secret-0123"
GRANT_PY = pathlib.Path(__file__).resolve().parents[3] / "e2e" / "init" / "grant.py"


@pytest.fixture(scope="module")
def grant():
    spec = importlib.util.spec_from_file_location("e2e_grant", GRANT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_AUTH_SECRET", SECRET)
    for name in ("INTERNAL_AUTH_WATCH_SECRET", "INTERNAL_AUTH_ADMIN_SECRET", "INTERNAL_AUTH_AUTHZ_SECRET"):
        monkeypatch.delenv(name, raising=False)
    from app import config
    monkeypatch.setattr(config.settings, "INTERNAL_AUTH_SECRET", SECRET, raising=False)


def test_a_minted_grant_verifies_with_its_claims(grant):
    token = grant.mint("sync", secret=SECRET, subject="e2e-runner", platform="github", datasource="mongo")
    claims = tokens.verify(token, tokens.AUDIENCE_SYNC)
    assert (claims.subject, claims.platform, claims.datasource, claims.tenant) == ("e2e-runner", "github", "mongo", "")


def test_a_grant_for_a_tenant_carries_it(grant):
    token = grant.mint("sync", secret=SECRET, subject="e2e-runner", tenant="payments")
    assert tokens.verify(token, tokens.AUDIENCE_SYNC).tenant == "payments"


def test_the_same_claims_as_the_orchestrators_own_minting(grant):
    mine = grant.mint("sync", secret=SECRET, subject="s", tenant="t", platform="p", datasource="d", now=1_000_000)
    theirs = tokens.mint("sync", secret=SECRET, subject="s", tenant="t", platform="p", datasource="d",
                         ttl_seconds=60, now=1_000_000)
    assert mine == theirs      # byte for byte: same header, claims, encoding and signature


def test_a_grant_signed_with_another_key_is_refused(grant):
    token = grant.mint("sync", secret="another-secret-another-secret-0123456789")
    with pytest.raises(tokens.GrantError, match="invalid signature"):
        tokens.verify(token, tokens.AUDIENCE_SYNC)


def test_an_expired_grant_is_refused(grant):
    token = grant.mint("sync", secret=SECRET, ttl=60, now=time.time() - 3600)
    with pytest.raises(tokens.GrantError, match="token has expired"):
        tokens.verify(token, tokens.AUDIENCE_SYNC)


def test_a_grant_for_another_service_is_refused(grant):
    token = grant.mint("watch", secret=SECRET)
    with pytest.raises(tokens.GrantError, match="wrong audience"):
        tokens.verify(token, tokens.AUDIENCE_SYNC)


def test_a_changed_byte_is_refused(grant):
    token = grant.mint("sync", secret=SECRET, subject="e2e-runner")
    head, body, sig = token.split(".")
    forged = ".".join([head, body[:-2] + ("AA" if not body.endswith("AA") else "BB"), sig])
    with pytest.raises(tokens.GrantError):
        tokens.verify(forged, tokens.AUDIENCE_SYNC)


def test_redact_hides_a_bearer_token_and_the_secret(grant):
    token = grant.mint("sync", secret=SECRET)
    text = f"POST /x -H 'Authorization: Bearer {token}' secret={SECRET}"
    shown = grant.redact(text)
    assert token not in shown and SECRET not in shown and "Bearer ******" in shown


def test_an_unknown_audience_is_an_error_here_too(grant):
    with pytest.raises(ValueError):
        grant.mint("nobody", secret=SECRET)
