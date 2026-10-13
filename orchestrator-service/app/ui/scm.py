"""
app/ui/scm.py
──────────────
The Git platform as identity provider and as authority on what a user may
see. The web UI signs users in with the platform (OAuth2 authorization code
flow with PKCE) and then asks it, with the user's own token, which
repositories that user can read or write: the platform's permissions decide
which rules the UI shows, the way a cluster's RBAC decides what a Kubernetes
dashboard shows to an impersonated user.

GitHub (github.com or Enterprise) and Gitea are supported, matching the
platforms a bundle can declare.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

# Paging cap when listing a user's repositories (only needed for rules whose
# repositories are glob patterns).
MAX_REPO_PAGES = 20


@dataclass(frozen=True)
class Permission:
    read: bool = False
    write: bool = False

    def __or__(self, other: "Permission") -> "Permission":
        return Permission(self.read or other.read, self.write or other.write)


NO_ACCESS = Permission()
FULL_ACCESS = Permission(read=True, write=True)


@dataclass(frozen=True)
class ScmUser:
    login: str
    name: str
    email: str
    avatar_url: str
    is_admin: bool

    def public(self) -> dict[str, Any]:
        return {"login": self.login, "name": self.name or self.login, "email": self.email,
                "avatarUrl": self.avatar_url, "isAdmin": self.is_admin}


class ScmError(Exception):
    """The platform could not answer."""


class TokenRejected(ScmError):
    """The platform no longer accepts the user's token (expired or revoked)."""


def _permission(perms: dict | None) -> Permission:
    """The platform's repository permission block → read/write. A repository the
    user can fetch is readable even when the block is missing (public repos)."""
    perms = perms or {}
    write = any(perms.get(k) for k in ("admin", "maintain", "push"))
    return Permission(read=True, write=bool(write))


class Provider:
    """One Git platform: OAuth2 endpoints plus the API calls the UI needs."""

    kind = ""
    default_scopes: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        endpoint: str,
        public_url: str,
        client_id: str,
        client_secret: str,
        scopes: list[str] | None = None,
        verify_ssl: bool = True,
        timeout: float = 10.0,
    ) -> None:
        # endpoint: where the orchestrator reaches the platform; public_url: where
        # the browser does (they differ when the platform runs next to varTrack).
        self.endpoint = endpoint.rstrip("/")
        self.public_url = (public_url or endpoint).rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = list(scopes) if scopes else list(self.default_scopes)
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    # ── OAuth2 ────────────────────────────────────────────────────────────────

    def authorize_url(self, *, state: str, redirect_uri: str, code_challenge: str) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.public_url}/login/oauth/authorize?{urlencode(query)}"

    def exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        return self._token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        })

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{self.endpoint}/login/oauth/access_token",
                data={**form, "client_id": self.client_id, "client_secret": self.client_secret},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise ScmError(f"{self.kind}: token request failed: {exc}") from exc
        body = _json(resp)
        if resp.status_code >= 400 or "error" in body or not body.get("access_token"):
            raise ScmError(f"{self.kind}: token request rejected: "
                           f"{body.get('error_description') or body.get('error') or resp.status_code}")
        return body

    # ── API ───────────────────────────────────────────────────────────────────

    @property
    def api_url(self) -> str:
        raise NotImplementedError

    def _get(self, token: str, path: str, params: dict | None = None) -> requests.Response:
        try:
            resp = requests.get(
                f"{self.api_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise ScmError(f"{self.kind}: {path}: {exc}") from exc
        if resp.status_code == 401:
            raise TokenRejected(f"{self.kind}: token rejected")
        return resp

    def user(self, token: str) -> ScmUser:
        resp = self._get(token, "/user")
        if resp.status_code != 200:
            raise ScmError(f"{self.kind}: /user answered {resp.status_code}")
        return self._user(_json(resp))

    def _user(self, body: dict) -> ScmUser:
        raise NotImplementedError

    def permission(self, token: str, repo: str) -> Permission:
        """What the user may do with *repo* ("owner/name"). The platforms answer
        404 for a repository the user cannot see, whether or not it exists."""
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return NO_ACCESS
        resp = self._get(token, f"/repos/{owner}/{name}")
        if resp.status_code == 200:
            return _permission(_json(resp).get("permissions"))
        if resp.status_code in (403, 404):
            return NO_ACCESS
        raise ScmError(f"{self.kind}: /repos/{repo} answered {resp.status_code}")

    def repos(self, token: str) -> dict[str, Permission]:
        """Every repository the user can access, for rules that name repositories by pattern."""
        out: dict[str, Permission] = {}
        for page in range(1, MAX_REPO_PAGES + 1):
            resp = self._get(token, self._repos_path, {**self._repos_params, self._page_size_param: 100, "page": page})
            if resp.status_code != 200:
                raise ScmError(f"{self.kind}: {self._repos_path} answered {resp.status_code}")
            batch = _json(resp)
            if not isinstance(batch, list) or not batch:
                break
            for repo in batch:
                if repo.get("full_name"):
                    out[repo["full_name"]] = _permission(repo.get("permissions"))
            if len(batch) < 100:
                break
        return out

    _repos_path = "/user/repos"
    _repos_params: dict[str, Any] = {}
    _page_size_param = "per_page"


class GitHub(Provider):
    kind = "github"
    # OAuth Apps can only read private repositories with the "repo" scope.
    default_scopes = ("read:user", "user:email", "repo")
    _repos_params = {"affiliation": "owner,collaborator,organization_member"}

    @property
    def api_url(self) -> str:
        # Same rule as the gateway: github.com → api.github.com, Enterprise → /api/v3.
        if not self.endpoint or "github.com" in self.endpoint:
            return "https://api.github.com"
        return self.endpoint if self.endpoint.endswith("/api/v3") else f"{self.endpoint}/api/v3"

    def __init__(self, **kw: Any) -> None:
        if not kw.get("endpoint") or "api.github.com" in kw["endpoint"]:
            kw["endpoint"] = "https://github.com"
        super().__init__(**kw)

    def _user(self, body: dict) -> ScmUser:
        return ScmUser(login=body.get("login", ""), name=body.get("name") or "", email=body.get("email") or "",
                       avatar_url=body.get("avatar_url") or "", is_admin=bool(body.get("site_admin")))


class Gitea(Provider):
    kind = "gitea"
    default_scopes = ("read:user", "read:repository")
    _page_size_param = "limit"

    @property
    def api_url(self) -> str:
        return f"{self.endpoint}/api/v1"

    def _user(self, body: dict) -> ScmUser:
        return ScmUser(login=body.get("login", ""), name=body.get("full_name") or "", email=body.get("email") or "",
                       avatar_url=body.get("avatar_url") or "", is_admin=bool(body.get("is_admin")))


PROVIDERS: dict[str, type[Provider]] = {"github": GitHub, "gitea": Gitea}


def _json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}
