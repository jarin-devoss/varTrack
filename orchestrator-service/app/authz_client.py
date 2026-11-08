"""
app/authz_client.py
────────────────────
The orchestrator's only way of asking "may this person?".

Who may see and do what is decided by one permission engine, in the gateway, the
same engine the CLI API asks (docs/operations/internal-auth.md, docs/configuration/auth.md).
The web UI is an enforcement point: it gathers the facts (the rule as the tenant's
schema repository states it, the environment, the repository an environment was
synced from), asks here, and does what the answer says. Nothing in the orchestrator
turns a Git permission into a decision.

  identify        who a token belongs to on its platform, and whether they are an admin
  decide          one question: may this caller take this action on this rule?
  readable_rules  many rules at once: the ones the caller may read, and what they may do
  forget          drop what the engine remembers of a caller's tokens

Every call carries a signed "authz" grant (app/api/tokens.py). The platform tokens
travel as request fields, over TLS in production, and are never logged.

When the engine cannot be reached the callers answer 503: there is no local fallback,
because a second, quieter engine is exactly what this module exists to prevent.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

# An identity the engine is asked about: (platform name, the caller's token).
Identity = tuple[str, str]


class AuthzError(Exception):
    """The engine could not answer."""


class Unavailable(AuthzError):
    """The engine cannot be reached, or cannot ask the Git platform. Callers answer 503."""


class TokenRejected(AuthzError):
    """The Git platform does not accept a token the caller holds."""


class NoSuchRule(AuthzError):
    """The question names a rule the engine does not know."""


class BadQuestion(AuthzError):
    """The engine refused the question as ill-formed (for example, no platform named)."""


@dataclass(frozen=True)
class Who:
    platform: str
    login: str
    admin: bool


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    denied_by: str = ""
    login: str = ""
    platform: str = ""
    admin: bool = False
    permission: str = "none"
    # The branch a sync is judged on, and so the one a re-sync must read.
    branch: str = ""
    # The tenant the caller was judged to act for (declared, allowed by the rule, reached in Git).
    tenant: str = ""


@dataclass(frozen=True)
class RuleAccess:
    permission: str
    can_sync: bool


# ── facts ────────────────────────────────────────────────────────────────────

def rule_facts(rule: dict, envs: Iterable[str] = ()) -> Any:
    """The engine's view of a rule: where it syncs from. The repositories are the
    ones the pipeline checks every pushed payload against (app/pipeline/repo_scope.py),
    so the two cannot disagree about which repositories a rule covers."""
    from app.pipeline import repo_scope
    from vartrack.v1.services import authz_pb2  # type: ignore

    include, exclude = repo_scope.patterns(rule)
    return authz_pb2.RuleFacts(
        platform=rule.get("platform", ""),
        datasource=rule.get("datasource", ""),
        repositories=include,
        exclude_repositories=exclude,
        branch_map={str(k): str(v) for k, v in (rule.get("branch_map") or {}).items()},
        branch_as_env=bool(rule.get("branch_as_env")),
        # What feeds "default" and the file_path_map environments, and what self-heal reads: the rule's
        # own branch ("main" when it names none, as the heal does).
        branch=str(rule.get("branch") or "main"),
        file_path_envs=sorted(str(env) for env in (rule.get("file_path_map") or {})),
        envs=sorted(set(envs)),
        tenants=[str(t) for t in (rule.get("tenants") or [])],
    )


def _identities(identities: Sequence[Identity]) -> list[Any]:
    from vartrack.v1.services import authz_pb2  # type: ignore
    return [authz_pb2.Identity(platform=p, token=t) for p, t in identities]


# ── the connection ───────────────────────────────────────────────────────────

_GRANT_TTL_SECONDS = 60
_GRANT_REUSE_SECONDS = 30


class Client:
    """A connection to the gateway's Authz service."""

    def __init__(self, address: str, *, timeout: float = 10.0, channel: Any = None) -> None:
        self.address = address
        self.timeout = timeout
        self._channel = channel
        self._stub: Any = None
        self._lock = threading.Lock()
        self._grant: tuple[float, str] = (0.0, "")

    # -- transport ------------------------------------------------------------

    def _stub_for(self):
        with self._lock:
            if self._stub is None:
                from vartrack.v1.services import authz_pb2_grpc  # type: ignore
                self._stub = authz_pb2_grpc.AuthzStub(self._channel or self._connect())
            return self._stub

    def _connect(self):
        import grpc
        from app.config import settings

        if not settings.MTLS_ENABLED:
            return grpc.insecure_channel(self.address)

        def read(path: str) -> Optional[bytes]:
            if not path:
                return None
            with open(path, "rb") as fh:
                return fh.read()

        creds = grpc.ssl_channel_credentials(
            root_certificates=read(settings.GATEWAY_AUTHZ_CA_FILE or settings.TLS_CA_FILE),
            private_key=read(settings.TLS_KEY_FILE),
            certificate_chain=read(settings.TLS_CERT_FILE),
        )
        return grpc.secure_channel(self.address, creds)

    def _metadata(self) -> list[tuple[str, str]]:
        from app.api import tokens
        if not tokens.enforced():
            return []  # development: the gateway serves without checking grants too
        now = time.time()
        with self._lock:
            minted, grant = self._grant
            if not grant or now - minted > _GRANT_REUSE_SECONDS:
                grant = tokens.mint(tokens.AUDIENCE_AUTHZ, secret=tokens.key_for(tokens.AUDIENCE_AUTHZ),
                                    subject="orchestrator", ttl_seconds=_GRANT_TTL_SECONDS)
                self._grant = (now, grant)
        return [("authorization", f"Bearer {grant}")]

    def _call(self, method: str, request: Any):
        import grpc

        try:
            return getattr(self._stub_for(), method)(request, timeout=self.timeout, metadata=self._metadata())
        except grpc.RpcError as exc:
            raise _translate(exc) from None

    # -- questions ------------------------------------------------------------

    def identify(self, identity: Identity) -> Who:
        from vartrack.v1.services import authz_pb2  # type: ignore
        got = self._call("Identify", authz_pb2.IdentifyRequest(identity=_identities([identity])[0]))
        return Who(platform=got.platform, login=got.login, admin=got.admin)

    def decide(self, identities: Sequence[Identity], action: str, *, rule: Optional[dict] = None,
               envs: Iterable[str] = (), env: str = "", source_repo: str = "", tenant: str = "",
               file_path: str = "", via: str = "ui") -> Decision:
        from vartrack.v1.services import authz_pb2  # type: ignore
        request = authz_pb2.DecideRequest(
            identities=_identities(identities), action=action, env=env, source_repo=source_repo,
            tenant_id=tenant, file_path=file_path, via=via,
        )
        if rule is not None:
            request.rule.CopyFrom(rule_facts(rule, envs))
        got = self._call("Decide", request)
        return Decision(allowed=got.allowed, reason=got.reason, denied_by=got.denied_by, login=got.login,
                        platform=got.platform, admin=got.admin, permission=got.permission or "none",
                        branch=got.branch, tenant=got.tenant)

    def readable_rules(self, identities: Sequence[Identity], rules: Sequence[dict],
                       envs: Optional[Sequence[Iterable[str]]] = None,
                       tenant: str = "") -> tuple[bool, dict[int, RuleAccess]]:
        """(is the caller an admin, {index into rules: what they may do}) for the
        rules they may read; every other rule is left out."""
        from vartrack.v1.services import authz_pb2  # type: ignore
        envs = envs or [()] * len(rules)
        request = authz_pb2.ReadableRulesRequest(
            identities=_identities(identities),
            rules=[rule_facts(r, e) for r, e in zip(rules, envs)],
            tenant_id=tenant,
        )
        got = self._call("ReadableRules", request)
        return got.admin, {r.index: RuleAccess(permission=r.permission, can_sync=r.can_sync) for r in got.rules}

    def forget(self, identities: Sequence[Identity]) -> None:
        from vartrack.v1.services import authz_pb2  # type: ignore
        self._call("Forget", authz_pb2.ForgetRequest(identities=_identities(identities)))


def _translate(exc) -> AuthzError:
    """A gRPC failure as the exception it means. The gateway's own messages are
    safe to pass on: they hold a platform's URL and status, never a token."""
    import grpc

    code = exc.code()
    detail = (exc.details() or "").strip()
    if code == grpc.StatusCode.UNAUTHENTICATED:
        if "rejected the token" in detail:
            return TokenRejected(detail)
        # The engine did not accept this service's grant (wrong key, clock skew).
        logger.error("authz: the permission engine refused this service's grant: %s", detail)
        return Unavailable("the permission engine refused this service's credentials")
    if code == grpc.StatusCode.NOT_FOUND:
        return NoSuchRule(detail)
    if code == grpc.StatusCode.INVALID_ARGUMENT:
        return BadQuestion(detail)
    if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
        logger.warning("authz: the permission engine is unreachable: %s", detail)
        return Unavailable("the permission engine is unreachable")
    logger.error("authz: the permission engine failed (%s): %s", code, detail)
    return Unavailable(f"the permission engine failed: {code.name.lower()}")


# ── the shared client ────────────────────────────────────────────────────────

_client: Optional[Client] = None
_client_lock = threading.Lock()


def client() -> Client:
    """The client for GATEWAY_AUTHZ_ADDR. Unavailable when it is not set: the web UI
    with sign-in on cannot judge anyone without the engine, and does not try."""
    global _client
    from app.config import settings

    with _client_lock:
        if _client is None:
            if not settings.GATEWAY_AUTHZ_ADDR:
                raise Unavailable("GATEWAY_AUTHZ_ADDR is not set: the web UI cannot ask the permission engine")
            _client = Client(settings.GATEWAY_AUTHZ_ADDR, timeout=settings.GATEWAY_AUTHZ_TIMEOUT)
        return _client


def set_client(replacement: Optional[Client]) -> None:
    """Use *replacement* from now on (tests); None forgets the shared client."""
    global _client
    with _client_lock:
        _client = replacement
