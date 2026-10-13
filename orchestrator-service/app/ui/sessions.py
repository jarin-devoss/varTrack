"""
app/ui/sessions.py
───────────────────
Single sign-on for the web UI through the Git platforms, and the sessions it
creates.

Configured in the bundle (config.cue), in the ui block — see
proto/vartrack/v1/models/ui.proto:

  ui: {
    public_url: "https://vartrack.example.com"
    auth: {
      providers: [
        {platform: "github", client_id: "Iv1.8a61…", client_secret: {file: "/run/secrets/github-oauth"}},
        {platform: "gitea",  client_id: "c2f3…",     client_secret: "…", browser_url: "http://localhost:3000"},
      ]
      admins: ["github:octo-admin"]
    }
  }

Each provider is the OAuth application registered on one of the bundle's
platforms. A user signs in with one and can connect the others to the same
session; each rule is then judged on its own platform with that platform's
account. Without ui.auth the UI is open, as before.

Without a ui.auth block, the same settings are read from UI_AUTH_* environment
variables (UI_AUTH_PLATFORMS, UI_AUTH_{P}_CLIENT_ID, …, see docs/configuration/auth.md).

The browser holds only a random session id in an HttpOnly cookie. The users'
platform tokens stay server-side, encrypted, in Redis next to the ledger —
never in the browser.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ui import ledger, scm

logger = logging.getLogger(__name__)

SESSION_COOKIE = "vt_session"
STATE_COOKIE = "vt_oauth_state"
LOGIN_STATE_TTL_SECONDS = 300
DEFAULT_PUBLIC_URL = "http://localhost:5173"
DEFAULT_SESSION_TTL_SECONDS = 8 * 3600


class AuthConfigError(Exception):
    """Sign-in is configured but cannot be used."""


@dataclass(frozen=True)
class AuthConfig:
    platforms: dict[str, scm.Provider]   # bundle platform name → its OAuth provider, in sign-in order
    redirect_uri: str
    secure_cookies: bool
    session_ttl: int
    admins: frozenset[str]               # "platform:login", lower-case
    key: bytes                           # AES-256 key for sessions at rest

    def is_admin(self, platform: str, login: str) -> bool:
        return f"{platform}:{login}".lower() in self.admins


@dataclass
class _Provider:
    """One sign-in platform as configured, before it is checked against the bundle."""
    platform: str
    client_id: str
    client_secret: str
    browser_url: str = ""
    scopes: list[str] = field(default_factory=list)


_config: tuple[float, AuthConfig | None | AuthConfigError] | None = None
_config_lock = threading.Lock()


def config() -> AuthConfig | None:
    """The SSO configuration, or None when the UI is open (no ui.auth)."""
    global _config
    with _config_lock:
        cached = _config
    if cached:
        age, value = time.time() - cached[0], cached[1]
        # A failure is kept briefly too, so a secret manager that is down
        # isn't asked again by every request while the UI says why.
        if isinstance(value, AuthConfigError):
            if age < 10:
                raise AuthConfigError(str(value))
        elif age < 60:
            return value
    try:
        cfg = _load_config()
    except AuthConfigError as exc:
        with _config_lock:
            _config = (time.time(), exc)
        raise
    with _config_lock:
        _config = (time.time(), cfg)
    return cfg


def _cue_bundle() -> dict:
    from app.config import _load_cue_bundle, settings as app_settings

    return (_load_cue_bundle(app_settings.CONFIG_PATH) if app_settings.CONFIG_PATH else None) or {}


def _load_config() -> AuthConfig | None:
    bundle = _cue_bundle()
    ui = bundle.get("ui") or {}
    if ui.get("auth"):
        return _from_bundle(bundle, ui)
    return _from_env(bundle)


# ── ui block in the bundle ────────────────────────────────────────────────────

class _SecretManagerRefs:
    """
    Reads {ref: {path, key}} values from one of the bundle's secret managers —
    ui.auth.secret_manager, or the only one there is. Connects on first use.
    """

    def __init__(self, bundle: dict, name: str) -> None:
        self.bundle = bundle
        self.name = name
        self._client: Any = None
        self._label = ""

    def get(self, ref: Any, where: str) -> str:
        path, key = _external_ref(ref, where)
        client = self._connect(where)
        try:
            value = client.get_secret(path, key)
        except Exception as exc:
            raise AuthConfigError(f"{where}: cannot read {path}#{key} from {self._label}: {exc}") from exc
        if not value:
            raise AuthConfigError(f"{where}: {path}#{key} in {self._label} is empty")
        return str(value).strip()

    def _connect(self, where: str) -> Any:
        if self._client is not None:
            return self._client
        managers = [((f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind), kind, cfg)
                    for wrapper in self.bundle.get("secret_managers") or [] for kind, cfg in wrapper.items()]
        if self.name:
            found = [m for m in managers if m[0] == self.name]
            if not found:
                raise AuthConfigError(f"ui.auth.secret_manager {self.name!r} is not in secret_managers")
        elif len(managers) == 1:
            found = managers
        elif not managers:
            raise AuthConfigError(f"{where} refers to a secret manager, but the bundle has no secret_managers")
        else:
            raise AuthConfigError(f"{where}: the bundle has several secret managers — "
                                  f"name one with ui.auth.secret_manager ({', '.join(m[0] for m in managers)})")
        name, kind, cfg = found[0]
        if kind != "vault":
            raise AuthConfigError(f"secret manager {name!r} is a {kind!r}; references are read from vault only")
        from app.utils.vault_client import VaultClient

        try:
            self._client = VaultClient(
                endpoint=cfg["endpoint"],
                mount_point=cfg.get("mount_point") or "secret",
                kv_version=int(cfg.get("kv_version") or 2),
                namespace=cfg.get("namespace") or None,
                auth=_vault_auth(cfg, name),
                verify_ssl=bool(cfg.get("verify_ssl", True)),
                ssl_ca=cfg.get("ssl_ca") or None,
                timeout=_seconds(cfg.get("timeout"), 10),
                max_retries=int(cfg.get("max_retries") or 3),
            )
        except AuthConfigError:
            raise
        except Exception as exc:
            raise AuthConfigError(f"cannot connect to secret manager {name!r}: {exc}") from exc
        self._label = f"secret manager {name!r}"
        return self._client


def _external_ref(ref: Any, where: str) -> tuple[str, str]:
    """{ref: {path, key}} as (path, key)."""
    inner = ref.get("ref") if isinstance(ref, dict) else None
    path = str(inner.get("path") or "") if isinstance(inner, dict) else ""
    key = str(inner.get("key") or "") if isinstance(inner, dict) else ""
    if not path or not key:
        raise AuthConfigError(f"{where}: a reference needs a path and a key, e.g. {{ref: {{path: \"ui/gitea\", key: \"client_secret\"}}}}")
    return path, key


def _vault_auth(cfg: dict, name: str) -> dict:
    """The bundle's vault auth block (vault.proto) in the form VaultClient takes."""
    from app.config import _resolve_secret_ref

    plain = lambda v: _resolve_secret_ref(v) or ""  # noqa: E731 — the fields are strings; {file:} is accepted too
    if cfg.get("token_auth"):
        return {"token": plain(cfg["token_auth"].get("token"))}
    if cfg.get("app_role_auth"):
        a = cfg["app_role_auth"]
        return {"app_role": {"role_id": plain(a.get("role_id")), "secret_id": plain(a.get("secret_id")),
                             **({"mount_path": a["mount_path"]} if a.get("mount_path") else {})}}
    if cfg.get("kubernetes_auth"):
        a = cfg["kubernetes_auth"]
        return {"kubernetes": {k: a[k] for k in ("role", "jwt_path", "mount_path") if a.get(k)}}
    if cfg.get("userpass_auth"):
        a = cfg["userpass_auth"]
        return {"userpass": {"username": plain(a.get("username")), "password": plain(a.get("password")),
                             **({"mount_path": a["mount_path"]} if a.get("mount_path") else {})}}
    raise AuthConfigError(f"secret manager {name!r} has no auth block (token_auth, app_role_auth, …)")


def _seconds(value: Any, default: int) -> int:
    """A proto Duration from CUE ("10s") or a number, in whole seconds."""
    try:
        return max(1, int(float(str(value).rstrip("s")))) if value not in (None, "") else default
    except ValueError:
        return default


def _secret(ref: Any, where: str, refs: _SecretManagerRefs | None = None, *, required: bool = True) -> str:
    """A SecretRef from the bundle: "value", {value: …}, {file: …} or {ref: …}."""
    from app.config import _resolve_secret_ref

    if ref in (None, "", {}):
        if required:
            raise AuthConfigError(f"{where} is required")
        return ""
    if isinstance(ref, dict) and "ref" in ref:
        if refs is None:
            raise AuthConfigError(f"{where}: a secret-manager reference is not supported here; use a value or {{file: …}}")
        return refs.get(ref, where)
    try:
        value = _resolve_secret_ref(ref)
    except ValueError as exc:
        raise AuthConfigError(f"{where}: {exc}") from exc
    if value is None:
        raise AuthConfigError(f"{where}: expected a string, {{value: …}}, {{file: …}} or {{ref: …}}")
    return value.strip()


def _from_bundle(bundle: dict, ui: dict) -> AuthConfig:
    auth = ui["auth"]
    refs = _SecretManagerRefs(bundle, str(auth.get("secret_manager") or "").strip())
    providers = []
    for i, p in enumerate(auth.get("providers") or []):
        name = str(p.get("platform") or "").strip()
        if not name:
            raise AuthConfigError(f"ui.auth.providers[{i}].platform is required")
        where = f"ui.auth.providers[{name}]"
        providers.append(_Provider(
            platform=name,
            client_id=_secret(p.get("client_id"), f"{where}.client_id", refs),
            client_secret=_secret(p.get("client_secret"), f"{where}.client_secret", refs),
            browser_url=str(p.get("browser_url") or "").strip(),
            scopes=[str(s) for s in p.get("scopes") or []],
        ))
    if not providers:
        raise AuthConfigError("ui.auth.providers needs at least one platform")
    return _assemble(
        bundle, providers,
        public_url=str(ui.get("public_url") or DEFAULT_PUBLIC_URL),
        redirect_url=str(auth.get("redirect_url") or ""),
        admins=[str(a) for a in auth.get("admins") or []],
        session_ttl=int(auth.get("session_ttl_seconds") or DEFAULT_SESSION_TTL_SECONDS),
        session_secret=_secret(auth.get("session_secret"), "ui.auth.session_secret", refs, required=False),
        admins_setting="ui.auth.admins",
    )


# ── UI_AUTH_* environment variables (when the bundle has no ui.auth) ──────────

def env_name(platform: str) -> str:
    """"github-enterprise" → "GITHUB_ENTERPRISE", as used in UI_AUTH_{P}_*."""
    return "".join(c if c.isalnum() else "_" for c in platform).upper()


def _from_env(bundle: dict) -> AuthConfig | None:
    names = [n.strip() for n in os.getenv("UI_AUTH_PLATFORMS", "").split(",") if n.strip()]
    if not names:
        names = [n for n in [os.getenv("UI_AUTH_PLATFORM", "").strip()] if n]
    if not names:
        return None

    providers = []
    for name in dict.fromkeys(names):
        def setting(key: str) -> str:
            # One platform: the unprefixed UI_AUTH_CLIENT_ID etc. do too. Each can
            # also be read from a file (VAR_FILE), for Docker and Kubernetes secrets.
            for var in [f"UI_AUTH_{env_name(name)}_{key}", *([f"UI_AUTH_{key}"] if len(names) == 1 else [])]:
                value = os.getenv(var, "").strip()
                if value:
                    return value
                path = os.getenv(f"{var}_FILE", "").strip()
                if path:
                    try:
                        return Path(path).read_text(encoding="utf-8").strip()
                    except OSError as exc:
                        raise AuthConfigError(f"{var}_FILE: cannot read {path}: {exc.strerror}") from exc
            return ""

        client_id, client_secret = setting("CLIENT_ID"), setting("CLIENT_SECRET")
        if not client_id or not client_secret:
            raise AuthConfigError(f"sign-in with {name!r} needs UI_AUTH_{env_name(name)}_CLIENT_ID and "
                                  f"UI_AUTH_{env_name(name)}_CLIENT_SECRET")
        providers.append(_Provider(platform=name, client_id=client_id, client_secret=client_secret,
                                   browser_url=setting("URL"), scopes=setting("SCOPES").replace(",", " ").split()))

    return _assemble(
        bundle, providers,
        public_url=os.getenv("UI_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        redirect_url=os.getenv("UI_AUTH_REDIRECT_URL", "").strip(),
        admins=[a for a in os.getenv("UI_AUTH_ADMINS", "").split(",")],
        session_ttl=int(os.getenv("UI_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS))),
        session_secret=os.getenv("UI_SESSION_SECRET", "").strip(),
        admins_setting="UI_AUTH_ADMINS",
    )


# ── shared ────────────────────────────────────────────────────────────────────

def _assemble(bundle: dict, providers: list[_Provider], *, public_url: str, redirect_url: str,
              admins: list[str], session_ttl: int, session_secret: str, admins_setting: str) -> AuthConfig:
    platforms: dict[str, scm.Provider] = {}
    for p in providers:
        if p.platform in platforms:
            raise AuthConfigError(f"sign-in platform {p.platform!r} is configured twice")
        kind, settings = _bundle_platform(bundle, p.platform, p.browser_url)
        provider_cls = scm.PROVIDERS.get(kind)
        if provider_cls is None:
            raise AuthConfigError(f"platform {p.platform!r} is a {kind!r} platform; sign-in supports {sorted(scm.PROVIDERS)}")
        platforms[p.platform] = provider_cls(
            endpoint=settings.get("endpoint", ""),
            public_url=p.browser_url or settings.get("endpoint", ""),
            client_id=p.client_id,
            client_secret=p.client_secret,
            scopes=p.scopes or None,
            verify_ssl=bool(settings.get("verify_ssl", True)),
        )

    admin_set = set()
    for entry in (a.strip().lower() for a in admins if a.strip()):
        if ":" in entry:
            admin_set.add(entry)
        elif len(platforms) == 1:
            admin_set.add(f"{next(iter(platforms)).lower()}:{entry}")
        else:
            # "alice" on GitHub and "alice" on Gitea need not be the same person.
            raise AuthConfigError(f"{admins_setting} entry {entry!r} must name its platform, e.g. gitea:{entry}")

    redirect_uri = redirect_url or f"{public_url.rstrip('/')}/api/orchestrator/v1/auth/callback"
    return AuthConfig(
        platforms=platforms,
        redirect_uri=redirect_uri,
        secure_cookies=redirect_uri.startswith("https://"),
        session_ttl=session_ttl,
        admins=frozenset(admin_set),
        key=_derive_key(session_secret or "\n".join(p.client_secret for p in providers)),
    )


def _bundle_platform(bundle: dict, name: str, browser_url: str) -> tuple[str, dict]:
    """(kind, settings) of the bundle platform named *name* ("kind" or "kind-tag")."""
    for wrapper in bundle.get("platforms", []):
        for kind, cfg in wrapper.items():
            if (f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind) == name:
                return kind, cfg
    # Not in the bundle: a bare kind with its address given directly.
    if name in scm.PROVIDERS and (browser_url or name == "github"):
        return name, {"endpoint": browser_url}
    raise AuthConfigError(f"sign-in platform {name!r} is not a platform in the bundle")


def _derive_key(secret: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"vartrack ui session encryption").derive(secret.encode("utf-8"))


def _seal(key: bytes, data: dict) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return base64.urlsafe_b64encode(nonce + AESGCM(key).encrypt(nonce, json.dumps(data).encode(), None)).decode()


def _open(key: bytes, blob: str) -> dict | None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.urlsafe_b64decode(blob.encode())
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))
    except (InvalidTag, ValueError):
        return None


def _session_key(session_id: str) -> str:
    # Keyed by a hash, so reading Redis does not hand out usable cookies.
    return "vt:ui:session:" + hashlib.sha256(session_id.encode()).hexdigest()


# ── Sessions ──────────────────────────────────────────────────────────────────
#
# {"identities": {platform: {"user": {...}, "accessToken": …, "refreshToken": …,
#                            "connectedAt": …}},
#  "primary": platform signed in with first, "startedAt": …}

def _identity(user: scm.ScmUser, token: dict[str, Any]) -> dict[str, Any]:
    return {"user": user.public(), "accessToken": token["access_token"],
            "refreshToken": token.get("refresh_token") or "", "connectedAt": ledger.now_iso()}


def create_session(cfg: AuthConfig, *, platform: str, user: scm.ScmUser, token: dict[str, Any]) -> str:
    session_id = secrets.token_urlsafe(32)
    data = {"identities": {platform: _identity(user, token)}, "primary": platform, "startedAt": ledger.now_iso()}
    ledger._redis().set(_session_key(session_id), _seal(cfg.key, data), ex=cfg.session_ttl)
    return session_id


def connect_identity(cfg: AuthConfig, session_id: str, session: dict, *, platform: str,
                     user: scm.ScmUser, token: dict[str, Any]) -> None:
    """Add (or replace) the account on *platform* in an existing session."""
    session["identities"][platform] = _identity(user, token)
    update_session(cfg, session_id, session)


def get_session(cfg: AuthConfig, session_id: str | None) -> dict | None:
    if not session_id:
        return None
    blob = ledger._redis().get(_session_key(session_id))
    data = _open(cfg.key, blob) if blob else None
    if not data:
        return None
    if "identities" not in data and data.get("platform"):  # one-platform session from before
        data = {"identities": {data["platform"]: {k: data[k] for k in ("user", "accessToken", "refreshToken")}},
                "primary": data["platform"], "startedAt": data.get("startedAt")}
    # Only platforms still configured for sign-in count.
    data["identities"] = {p: i for p, i in data.get("identities", {}).items() if p in cfg.platforms}
    if not data["identities"]:
        return None
    if data.get("primary") not in data["identities"]:
        data["primary"] = next(iter(data["identities"]))
    return data


def update_session(cfg: AuthConfig, session_id: str, data: dict) -> None:
    ledger._redis().set(_session_key(session_id), _seal(cfg.key, data), keepttl=True)


def delete_session(session_id: str | None) -> None:
    if session_id:
        ledger._redis().delete(_session_key(session_id))


# ── Login state (platform, PKCE verifier and return path between login and callback) ──

def start_login(platform: str, return_to: str) -> tuple[str, str]:
    """(state, code_challenge). The state is single-use and expires in five minutes."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    ledger._redis().set(f"vt:ui:oauth:{state}",
                        json.dumps({"platform": platform, "verifier": verifier, "returnTo": return_to}),
                        ex=LOGIN_STATE_TTL_SECONDS, nx=True)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return state, challenge


def finish_login(state: str) -> dict | None:
    """The login started with *state*, consumed so a callback can be used once only."""
    raw = ledger._redis().getdel(f"vt:ui:oauth:{state}") if state else None
    return json.loads(raw) if raw else None


def safe_return_path(path: str | None) -> str:
    """A path in the UI to return to after sign-in; anything else becomes "/" (no open redirects)."""
    if not path or not path.startswith("/") or path.startswith(("//", "/\\")):
        return "/"
    if any(ord(c) < 33 for c in path[:2]) or "://" in path.split("?", 1)[0]:
        return "/"
    return path
