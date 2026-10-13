"""
app/ui/access.py
─────────────────
What the person looking at the web UI may see and do.

With single sign-on off, everyone sees everything (as before). With it on,
each rule is judged on its own Git platform, asked with the account the user
connected there:

  a rule        is visible when the user can read at least one repository
                it covers (its repository names and patterns, minus its
                exclusions, plus the repositories its environments were
                synced from);
  an environment is visible when the user can read the repository it was
                last synced from;
  syncing       a rule needs write access to one of those repositories
                (a dry run writes nothing and needs read access only).

A rule on a platform the user has not connected (or that sign-in does not
cover) is not visible: nothing can vouch for it. "platform:login" entries in
UI_AUTH_ADMINS see everything. Answers are cached per session for
PERMISSION_TTL_SECONDS.
"""
from __future__ import annotations

import fnmatch
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from app.ui import scm, sessions

logger = logging.getLogger(__name__)

PERMISSION_TTL_SECONDS = 120.0
_GLOB_CHARS = set("*?[")

# (session id, platform, repo or "*") → (fetched at, answer)
_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class Unauthenticated(Exception):
    """Single sign-on is on and the request has no valid session."""


class Viewer:
    """Everyone, when single sign-on is off."""

    authenticated = False
    user: dict | None = None

    def rule_permission(self, rule: dict, records: Iterable[dict] = ()) -> scm.Permission:
        return scm.FULL_ACCESS

    def can_see_target(self, rule: dict, record: dict) -> bool:
        return True

    def visible_rules(self, rules: list[dict], records: list[dict]) -> list[tuple[dict, scm.Permission]]:
        return [(rule, scm.FULL_ACCESS) for rule in rules]


class UserViewer(Viewer):
    """A signed-in user; each connected Git platform answers for its rules."""

    authenticated = True

    def __init__(self, cfg: sessions.AuthConfig, session_id: str, session: dict) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.session = session
        self.identities: dict[str, dict] = session["identities"]
        self.user = self.identities[session["primary"]]["user"]
        self.is_admin = any(cfg.is_admin(p, i["user"]["login"]) for p, i in self.identities.items())

    # ── platform calls, cached and retried once with a refreshed token ────────

    def _call(self, platform: str, fn, *args):
        identity = self.identities[platform]
        try:
            return fn(identity["accessToken"], *args)
        except scm.TokenRejected:
            if not identity.get("refreshToken"):
                raise Unauthenticated(f"{platform} no longer accepts this session")
            try:
                token = self.cfg.platforms[platform].refresh(identity["refreshToken"])
            except scm.ScmError as exc:
                raise Unauthenticated(str(exc)) from exc
            identity.update(accessToken=token["access_token"],
                            refreshToken=token.get("refresh_token") or identity["refreshToken"])
            sessions.update_session(self.cfg, self.session_id, self.session)
            return fn(identity["accessToken"], *args)

    def _cached(self, platform: str, key: str, compute):
        now = time.time()
        with _cache_lock:
            hit = _cache.get((self.session_id, platform, key))
        if hit and now - hit[0] < PERMISSION_TTL_SECONDS:
            return hit[1]
        value = compute()
        with _cache_lock:
            if len(_cache) > 10_000:  # drop expired answers of sessions long gone
                for stale in [k for k, (t, _) in _cache.items() if now - t >= PERMISSION_TTL_SECONDS]:
                    del _cache[stale]
            _cache[(self.session_id, platform, key)] = (now, value)
        return value

    def permission(self, platform: str, repo: str) -> scm.Permission:
        if self.is_admin:
            return scm.FULL_ACCESS
        if platform not in self.identities:
            return scm.NO_ACCESS
        provider = self.cfg.platforms[platform]

        def ask() -> scm.Permission:
            try:
                return self._call(platform, provider.permission, repo)
            except scm.ScmError as exc:
                logger.warning("ui access: %s %s for %s: %s", platform, repo, self.identities[platform]["user"]["login"], exc)
                return scm.NO_ACCESS  # unknown is not allowed
        return self._cached(platform, repo.lower(), ask)

    def accessible_repos(self, platform: str) -> dict[str, scm.Permission]:
        if platform not in self.identities:
            return {}
        provider = self.cfg.platforms[platform]

        def ask() -> dict[str, scm.Permission]:
            try:
                return self._call(platform, provider.repos)
            except scm.ScmError as exc:
                logger.warning("ui access: listing %s repositories for %s: %s", platform,
                               self.identities[platform]["user"]["login"], exc)
                return {}
        return self._cached(platform, "*", ask)

    # ── rules and environments ────────────────────────────────────────────────

    def rule_permission(self, rule: dict, records: Iterable[dict] = ()) -> scm.Permission:
        if self.is_admin:
            return scm.FULL_ACCESS
        platform = rule.get("platform", "")
        if platform not in self.identities:
            return scm.NO_ACCESS
        include, exclude = _patterns(rule)
        perm = scm.NO_ACCESS
        for repo in _named_repos(rule, records):
            if not _excluded(repo, exclude):
                perm |= self.permission(platform, repo)
                if perm.write:
                    return perm
        if not perm.read and any(_is_glob(p) for p in include):
            for repo, repo_perm in self.accessible_repos(platform).items():
                if _matches(repo, include) and not _excluded(repo, exclude):
                    perm |= repo_perm
        return perm

    def can_see_target(self, rule: dict, record: dict) -> bool:
        if self.is_admin:
            return True
        repo = record.get("repository")
        return self.permission(rule.get("platform", ""), repo).read if repo else self.rule_permission(rule).read

    def visible_rules(self, rules: list[dict], records: list[dict]) -> list[tuple[dict, scm.Permission]]:
        if self.is_admin:
            return [(rule, scm.FULL_ACCESS) for rule in rules]
        # Ask every platform about every repository at once, then decide from the cache.
        asks = {(rule["platform"], repo) for rule in rules if rule.get("platform") in self.identities
                for repo in _named_repos(rule, records)}
        if asks:
            with ThreadPoolExecutor(max_workers=min(8, len(asks)), thread_name_prefix="ui_access") as pool:
                list(pool.map(lambda a: self.permission(*a), asks))
        out = []
        for rule in rules:
            perm = self.rule_permission(rule, records)
            if perm.read:
                out.append((rule, perm))
        return out


def viewer(session_id: str | None) -> Viewer:
    """The viewer for a request. Raises Unauthenticated when sign-in is on and there is no valid session."""
    cfg = sessions.config()
    if cfg is None:
        return Viewer()
    session = sessions.get_session(cfg, session_id)
    if session is None or not session_id:
        raise Unauthenticated("sign in required")
    return UserViewer(cfg, session_id, session)


def forget(session_id: str | None) -> None:
    """Drop cached answers for a session (sign-out, or a platform connected)."""
    with _cache_lock:
        for key in [k for k in _cache if k[0] == session_id]:
            del _cache[key]


# ── repository patterns (same sources as the gateway's rule matching) ─────────

def _patterns(rule: dict) -> tuple[list[str], list[str]]:
    include = list(rule.get("repositories") or [])
    exclude = list(rule.get("exclude_repositories") or [])
    for override in rule.get("overrides") or []:
        if override.get("enable"):
            include += override.get("match_repositories") or []
            exclude += override.get("exclude_repositories") or []
    if not include and rule.get("repo_url"):
        from app.ui import ledger
        include = [ledger.repo_name(rule["repo_url"])]
    return include, exclude


def _named_repos(rule: dict, records: Iterable[dict]) -> list[str]:
    """Repositories a rule names outright, and the ones its environments were synced from."""
    named = [p for p in _patterns(rule)[0] if not _is_glob(p)]
    synced = [r["repository"] for r in records
              if r.get("datasource") == rule.get("datasource") and r.get("repository")]
    return list(dict.fromkeys([*named, *synced]))


def _is_glob(pattern: str) -> bool:
    return any(c in _GLOB_CHARS for c in pattern)


def _matches(repo: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(repo.lower(), p.lower()) for p in patterns)


def _excluded(repo: str, exclude: list[str]) -> bool:
    return _matches(repo, exclude)
