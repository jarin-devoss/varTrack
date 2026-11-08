"""
app/ui/access.py
─────────────────
What the person looking at the web UI may see and do.

This module decides nothing. With single sign-on on, every question goes to the
gateway's permission engine (app/authz_client.py), the same engine the CLI API asks,
with the accounts the user connected to the Git platforms. The engine answers from
what each platform says about the user's own token, then from the optional policy:

  a rule         is visible when the user can read at least one repository it covers;
  an environment is visible when the user can read the repository it was last
                 synced from;
  syncing        needs what a direct push to the branch that feeds the environment
                 needs, branch protection included, and then the policy's consent
                 (a dry run needs read access only).

A rule on a platform the user has not connected is not visible: nothing can vouch for
it. Who the admins are is the engine's to say (config.cue's ui.auth.admins).

What stays here is the session: whose tokens go with a question, and refreshing one
the platform stopped accepting. When the engine cannot be reached the viewer raises
authz_client.Unavailable and the routes answer 503; there is no local fallback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

from app import authz_client
from app.ui import scm, sessions

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Unauthenticated(Exception):
    """Single sign-on is on and the request has no valid session."""


@dataclass(frozen=True)
class Access:
    """What a viewer may do with a rule, as the engine says."""
    read: bool = False
    can_sync: bool = False
    permission: str = "none"


FULL_ACCESS = Access(read=True, can_sync=True, permission="admin")


def _envs_of(rule: dict, records: Iterable[dict]) -> list[str]:
    return sorted({r["env"] for r in records if r.get("datasource") == rule.get("datasource") and r.get("env")})


class Viewer:
    """Everyone, when single sign-on is off: nothing is asked, nothing is refused."""

    authenticated = False
    user: dict | None = None
    is_admin = False
    identities: dict[str, dict] = {}

    def rule_access(self, rule: dict, records: Iterable[dict] = (), tenant: str = "") -> Access:
        return FULL_ACCESS

    def can_see_target(self, rule: dict, record: dict) -> bool:
        return True

    def visible_rules(self, rules: list[dict], records: list[dict], tenant: str = "") -> list[tuple[dict, Access]]:
        return [(rule, FULL_ACCESS) for rule in rules]

    def decide(self, rule: dict, action: str, env: str, tenant: str = "") -> authz_client.Decision:
        """Open: everything is allowed, if a branch feeds env. With no identity there is nothing to
        judge a branch by, so the one that feeds env is the rule's own mapping; an environment nothing
        feeds is refused, as the engine refuses it for everyone."""
        from app.pipeline import repo_scope
        branch = repo_scope.branch_for(rule, env)
        if not branch and env:
            return authz_client.Decision(allowed=False, permission="admin", denied_by="rule",
                                         reason="permission denied: " + repo_scope.nothing_feeds(rule, env))
        return authz_client.Decision(allowed=True, permission="admin", branch=branch)

    def forget(self) -> None:
        pass


class UserViewer(Viewer):
    """A signed-in user; the engine answers for each rule with the accounts they connected."""

    authenticated = True

    def __init__(self, cfg: sessions.AuthConfig, session_id: str, session: dict) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.session = session
        self.identities: dict[str, dict] = session["identities"]
        self.user = self.identities[session["primary"]]["user"]
        self._admin: bool | None = None
        # The answers to questions this request has already asked, so a page that
        # shows many environments of one repository asks once.
        self._seen: dict[tuple[str, str, str], bool] = {}

    # ── the session's tokens ──────────────────────────────────────────────────

    def _tokens(self) -> list[authz_client.Identity]:
        return [(platform, identity["accessToken"]) for platform, identity in self.identities.items()]

    def _ask(self, question: Callable[[], T]) -> T:
        """Ask the engine, once more after refreshing a token the platform stopped accepting."""
        try:
            return question()
        except authz_client.TokenRejected:
            self._refresh_rejected()
            try:
                return question()
            except authz_client.TokenRejected as exc:
                raise Unauthenticated("a connected platform no longer accepts this session") from exc

    def _refresh_rejected(self) -> None:
        """Find which connected account the platform rejects and refresh its token."""
        for platform, identity in self.identities.items():
            try:
                authz_client.client().identify((platform, identity["accessToken"]))
                continue
            except authz_client.TokenRejected:
                pass
            if not identity.get("refreshToken"):
                raise Unauthenticated(f"{platform} no longer accepts this session")
            try:
                token = self.cfg.platforms[platform].refresh(identity["refreshToken"])
            except scm.ScmError as exc:
                raise Unauthenticated(str(exc)) from exc
            identity.update(accessToken=token["access_token"],
                            refreshToken=token.get("refresh_token") or identity["refreshToken"])
            sessions.update_session(self.cfg, self.session_id, self.session)

    # ── what the engine says ──────────────────────────────────────────────────

    @property
    def is_admin(self) -> bool:  # type: ignore[override]
        if self._admin is None:
            self._admin = self._ask(lambda: any(
                authz_client.client().identify(identity).admin for identity in self._tokens()))
        return self._admin

    def visible_rules(self, rules: list[dict], records: list[dict], tenant: str = "") -> list[tuple[dict, Access]]:
        if not rules:
            return []
        envs = [_envs_of(rule, records) for rule in rules]
        admin, found = self._ask(lambda: authz_client.client().readable_rules(
            self._tokens(), rules, envs, tenant))
        self._admin = self._admin or admin
        return [(rule, Access(read=True, can_sync=found[i].can_sync, permission=found[i].permission))
                for i, rule in enumerate(rules) if i in found]

    def rule_access(self, rule: dict, records: Iterable[dict] = (), tenant: str = "") -> Access:
        found = self.visible_rules([rule], list(records), tenant)
        return found[0][1] if found else Access()

    def can_see_target(self, rule: dict, record: dict) -> bool:
        repo = record.get("repository")
        if not repo:
            # Synced from nowhere the ledger knows: as visible as the rule, which the
            # caller has already shown the viewer may read.
            return True
        key = (rule.get("platform", ""), rule.get("datasource", ""), repo)
        if key not in self._seen:
            decision = self._ask(lambda: authz_client.client().decide(
                self._tokens(), "read", rule=rule, source_repo=repo))
            self._seen[key] = decision.allowed
        return self._seen[key]

    def decide(self, rule: dict, action: str, env: str, tenant: str = "") -> authz_client.Decision:
        return self._ask(lambda: authz_client.client().decide(
            self._tokens(), action, rule=rule, env=env, tenant=tenant))

    def forget(self) -> None:
        try:
            authz_client.client().forget(self._tokens())
        except authz_client.AuthzError as exc:
            logger.warning("ui access: could not drop the engine's answers for a session: %s", exc)


def viewer(session_id: str | None) -> Viewer:
    """The viewer for a request. Raises Unauthenticated when sign-in is on and there is no valid session."""
    cfg = sessions.config()
    if cfg is None:
        return Viewer()
    session = sessions.get_session(cfg, session_id)
    if session is None or not session_id:
        raise Unauthenticated("sign in required")
    return UserViewer(cfg, session_id, session)


def forget(session_id: str | None, session: dict | None = None) -> None:
    """Ask the engine to drop what it remembers of a session's tokens (sign-out, or a
    platform connected anew). The tokens are the session's, so it must be given."""
    if not session:
        return
    cfg = sessions.config()
    if cfg is None:
        return
    try:
        UserViewer(cfg, session_id or "", session).forget()
    except Exception as exc:  # a half-formed session has nothing to forget
        logger.debug("ui access: nothing to forget for the session: %s", exc)
