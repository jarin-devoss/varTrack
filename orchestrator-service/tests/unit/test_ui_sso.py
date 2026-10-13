"""
tests/unit/test_ui_sso.py

Single sign-on for the web UI with the Git platforms, and what each signed-in
user may see: every rule is judged on its own platform, asked with the
account the user connected there.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

import app.tasks.etl  # noqa: F401 — the UI router imports it lazily
from app.ui import access, ledger, scm, sessions


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None, nx=False, keepttl=False):
        if nx and k in self.kv:
            return False
        self.kv[k] = v
        return True

    def getdel(self, k):
        return self.kv.pop(k, None)

    def delete(self, k):
        self.kv.pop(k, None)


class FakeProvider(scm.Provider):
    """A platform whose answers the test sets: token → repo → Permission."""

    def __init__(self, kind, public_url, login):
        self.kind = kind
        super().__init__(endpoint=public_url, public_url=public_url, client_id=f"{kind}-id", client_secret=f"{kind}-secret")
        self.login = login
        self.access: dict[str, dict[str, scm.Permission]] = {}
        self.calls: list[str] = []
        self.valid_tokens = {f"tok-{login}", f"tok-{login}-new"}
        self.verifier = None

    def exchange(self, *, code, redirect_uri, code_verifier):
        if code != "good-code":
            raise scm.ScmError("bad code")
        self.verifier = code_verifier
        return {"access_token": f"tok-{self.login}", "refresh_token": "ref"}

    def refresh(self, refresh_token):
        return {"access_token": f"tok-{self.login}-new", "refresh_token": "ref-new"}

    def _check(self, token):
        if token not in self.valid_tokens:
            raise scm.TokenRejected("expired")

    def user(self, token):
        self._check(token)
        return scm.ScmUser(login=self.login, name=self.login.title(), email="", avatar_url="", is_admin=False)

    def permission(self, token, repo):
        self._check(token)
        self.calls.append(repo)
        return self.access.get(repo, scm.NO_ACCESS)

    def repos(self, token):
        self._check(token)
        self.calls.append("*")
        return dict(self.access)


@pytest.fixture
def gitea():
    return FakeProvider("gitea", "http://localhost:3000", "alice")


@pytest.fixture
def github():
    return FakeProvider("github", "https://github.com", "alice-gh")


@pytest.fixture
def cfg(gitea, github, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ledger, "_redis", lambda: fake)
    access._cache.clear()
    conf = sessions.AuthConfig(platforms={"gitea": gitea, "github": github},
                               redirect_uri="http://ui.test/api/orchestrator/v1/auth/callback",
                               secure_cookies=False, session_ttl=3600, admins=frozenset({"gitea:root"}),
                               key=sessions._derive_key("secret"))
    monkeypatch.setattr(sessions, "config", lambda: conf)
    return conf


def _session(cfg, platform, token, login="alice"):
    user = scm.ScmUser(login=login, name=login, email="", avatar_url="", is_admin=False)
    sid = sessions.create_session(cfg, platform=platform, user=user, token={"access_token": token, "refresh_token": "ref"})
    return sid, access.viewer(sid)


RULES = [
    {"platform": "gitea", "datasource": "mongo", "repositories": ["acme/configs"]},
    {"platform": "gitea", "datasource": "redis", "repositories": ["acme/*"], "exclude_repositories": ["acme/secret*"]},
    {"platform": "gitea", "datasource": "s3", "repositories": ["payments/billing"]},
    {"platform": "github", "datasource": "zookeeper", "repositories": ["acme/configs"]},
    {"platform": "bitbucket", "datasource": "vercel", "repositories": ["acme/configs"]},
]
READ, WRITE = scm.Permission(read=True), scm.Permission(read=True, write=True)


def _names(visible):
    return {r["datasource"]: p for r, p in visible}


# ── the platform client ───────────────────────────────────────────────────────

class TestProviders:
    def test_authorize_url_uses_pkce_and_the_browser_facing_address(self):
        p = scm.Gitea(endpoint="http://gitea:3000", public_url="http://localhost:3000", client_id="c", client_secret="s")
        url = urlparse(p.authorize_url(state="st", redirect_uri="http://ui/cb", code_challenge="ch"))
        q = parse_qs(url.query)
        assert (url.scheme, url.netloc, url.path) == ("http", "localhost:3000", "/login/oauth/authorize")
        assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"] and q["response_type"] == ["code"]
        assert p.api_url == "http://gitea:3000/api/v1"

    @pytest.mark.parametrize("endpoint,api", [
        ("", "https://api.github.com"),
        ("https://github.com", "https://api.github.com"),
        ("https://api.github.com", "https://api.github.com"),
        ("https://ghe.acme.io", "https://ghe.acme.io/api/v3"),
        ("https://ghe.acme.io/api/v3", "https://ghe.acme.io/api/v3"),
        ("https://github.company.com", "https://github.company.com/api/v3"),  # Enterprise, not github.com
    ])
    def test_github_api_address(self, endpoint, api):
        assert scm.GitHub(endpoint=endpoint, public_url="", client_id="c", client_secret="s").api_url == api

    def test_gitea_api_address(self):
        assert scm.Gitea(endpoint="http://gitea:3000/", public_url="", client_id="c", client_secret="s").api_url == (
            "http://gitea:3000/api/v1")

    def test_platform_addresses_come_from_the_protos(self):
        assert scm.proto_const("github", "cloud_api_url") == "https://api.github.com"
        assert scm.proto_const("gitea", "api_path") == "/api/v1"

    def test_repository_permission_from_the_platform(self, monkeypatch):
        class Resp:
            def __init__(self, status, body=None):
                self.status_code, self._body = status, body or {}

            def json(self):
                return self._body

        answers = {
            "/repos/acme/rw": Resp(200, {"permissions": {"pull": True, "push": True}}),
            "/repos/acme/ro": Resp(200, {"permissions": {"pull": True, "push": False}}),
            "/repos/acme/hidden": Resp(404),
            "/repos/acme/expired": Resp(401),
        }
        monkeypatch.setattr(scm.requests, "get", lambda url, **kw: answers[url.split("/api/v1", 1)[1]])
        p = scm.Gitea(endpoint="http://gitea:3000", public_url="", client_id="c", client_secret="s")
        assert p.permission("t", "acme/rw") == WRITE
        assert p.permission("t", "acme/ro") == READ
        assert p.permission("t", "acme/hidden") == scm.NO_ACCESS
        with pytest.raises(scm.TokenRejected):
            p.permission("t", "acme/expired")


# ── configuration ─────────────────────────────────────────────────────────────

class TestConfig:
    @pytest.fixture(autouse=True)
    def bundle(self, monkeypatch):
        for k in [k for k in __import__("os").environ if k.startswith("UI_AUTH") or k.startswith("UI_SESSION") or k == "UI_PUBLIC_URL"]:
            monkeypatch.delenv(k)
        bundle = {"platforms": [
            {"gitea": {"endpoint": "http://gitea:3000"}},
            {"gitea": {"tag": "team", "endpoint": "http://gitea-team:3000"}},
            {"github": {"endpoint": "https://github.com"}},
            {"github": {"tag": "enterprise", "endpoint": "https://ghe.example"}},
        ]}
        monkeypatch.setattr(sessions, "_cue_bundle", lambda: bundle)
        return bundle

    def test_off_without_platforms(self):
        assert sessions._load_config() is None

    def test_the_ui_block_configures_sign_in(self, bundle, tmp_path):
        (tmp_path / "secret").write_text("from-a-file\n")
        bundle["ui"] = {"public_url": "https://vt.example", "auth": {
            "providers": [
                {"platform": "gitea", "client_id": "plain-id", "client_secret": {"file": str(tmp_path / "secret")},
                 "browser_url": "http://localhost:3000"},
                {"platform": "gitea-team", "client_id": {"value": "team-id"}, "client_secret": "team-secret",
                 "scopes": ["read:user"]},
            ],
            "admins": ["gitea:root"],
            "session_ttl_seconds": 3600,
        }}
        cfg = sessions._load_config()
        assert list(cfg.platforms) == ["gitea", "gitea-team"]
        gitea, team = cfg.platforms["gitea"], cfg.platforms["gitea-team"]
        assert (gitea.client_id, gitea.client_secret, gitea.public_url) == ("plain-id", "from-a-file", "http://localhost:3000")
        assert (team.client_id, team.client_secret, team.public_url, team.scopes) == (
            "team-id", "team-secret", "http://gitea-team:3000", ["read:user"])
        assert cfg.redirect_uri == "https://vt.example/api/orchestrator/v1/auth/callback" and cfg.secure_cookies
        assert cfg.session_ttl == 3600 and cfg.is_admin("gitea", "root")

    def test_the_ui_block_wins_over_the_environment(self, bundle, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORM", "github")
        monkeypatch.setenv("UI_AUTH_CLIENT_ID", "env-id")
        monkeypatch.setenv("UI_AUTH_CLIENT_SECRET", "env-secret")
        bundle["ui"] = {"auth": {"providers": [{"platform": "gitea", "client_id": "id", "client_secret": "s"}]}}
        assert list(sessions._load_config().platforms) == ["gitea"]

    @pytest.mark.parametrize("provider,error", [
        ({"platform": "gitea", "client_id": "id"}, "client_secret is required"),
        ({"platform": "gitea", "client_id": "id", "client_secret": {"ref": {"path": "ui", "key": "s"}}}, "no secret_managers"),
        ({"platform": "gitea", "client_id": "id", "client_secret": {"ref": {"path": "ui"}}}, "a path and a key"),
        ({"platform": "gitea", "client_id": "id", "client_secret": {"file": "/nowhere/secret"}}, "cannot read"),
        ({"platform": "bitbucket", "client_id": "id", "client_secret": "s"}, "not a platform in the bundle"),
    ])
    def test_a_provider_that_cannot_work_keeps_the_ui_closed(self, bundle, provider, error):
        bundle["ui"] = {"auth": {"providers": [provider]}}
        with pytest.raises(sessions.AuthConfigError, match=error):
            sessions._load_config()

    def test_several_platforms_each_with_their_own_application(self, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORMS", "gitea, github-enterprise")
        monkeypatch.setenv("UI_AUTH_GITEA_CLIENT_ID", "g-id")
        monkeypatch.setenv("UI_AUTH_GITEA_CLIENT_SECRET", "g-secret")
        monkeypatch.setenv("UI_AUTH_GITEA_URL", "http://localhost:3000")
        monkeypatch.setenv("UI_AUTH_GITHUB_ENTERPRISE_CLIENT_ID", "e-id")
        monkeypatch.setenv("UI_AUTH_GITHUB_ENTERPRISE_CLIENT_SECRET", "e-secret")
        monkeypatch.setenv("UI_AUTH_ADMINS", "gitea:root")
        cfg = sessions._load_config()
        assert list(cfg.platforms) == ["gitea", "github-enterprise"]
        assert cfg.platforms["gitea"].client_id == "g-id" and cfg.platforms["gitea"].public_url == "http://localhost:3000"
        assert cfg.platforms["github-enterprise"].kind == "github"
        assert cfg.is_admin("gitea", "Root") and not cfg.is_admin("github-enterprise", "root")

    def test_one_platform_keeps_the_short_settings(self, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORM", "gitea")
        monkeypatch.setenv("UI_AUTH_CLIENT_ID", "id")
        monkeypatch.setenv("UI_AUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("UI_AUTH_ADMINS", "root")  # one platform: a bare login is unambiguous
        cfg = sessions._load_config()
        assert list(cfg.platforms) == ["gitea"] and cfg.is_admin("gitea", "root")
        # Same key as before multi-platform support: existing sessions stay valid.
        assert cfg.key == sessions._derive_key("secret")

    def test_credentials_can_come_from_files(self, monkeypatch, tmp_path):
        (tmp_path / "id").write_text("file-id\n")  # trailing newline, as editors and secret mounts leave
        (tmp_path / "secret").write_text("file-secret\n")
        monkeypatch.setenv("UI_AUTH_PLATFORM", "gitea")
        monkeypatch.setenv("UI_AUTH_CLIENT_ID_FILE", str(tmp_path / "id"))
        monkeypatch.setenv("UI_AUTH_GITEA_CLIENT_SECRET_FILE", str(tmp_path / "secret"))
        provider = sessions._load_config().platforms["gitea"]
        assert (provider.client_id, provider.client_secret) == ("file-id", "file-secret")

    def test_a_missing_credentials_file_keeps_the_ui_closed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UI_AUTH_PLATFORM", "gitea")
        monkeypatch.setenv("UI_AUTH_CLIENT_ID", "id")
        monkeypatch.setenv("UI_AUTH_CLIENT_SECRET_FILE", str(tmp_path / "not-written-yet"))
        with pytest.raises(sessions.AuthConfigError, match="not-written-yet"):
            sessions._load_config()

    def test_a_platform_without_its_application_is_an_error(self, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORMS", "gitea,github")
        monkeypatch.setenv("UI_AUTH_GITEA_CLIENT_ID", "id")
        monkeypatch.setenv("UI_AUTH_GITEA_CLIENT_SECRET", "secret")
        with pytest.raises(sessions.AuthConfigError, match="UI_AUTH_GITHUB_CLIENT_ID"):
            sessions._load_config()

    def test_admins_must_name_their_platform_when_there_are_several(self, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORMS", "gitea,github")
        for p in ("GITEA", "GITHUB"):
            monkeypatch.setenv(f"UI_AUTH_{p}_CLIENT_ID", "id")
            monkeypatch.setenv(f"UI_AUTH_{p}_CLIENT_SECRET", "secret")
        monkeypatch.setenv("UI_AUTH_ADMINS", "alice")  # alice on GitHub need not be alice on Gitea
        with pytest.raises(sessions.AuthConfigError, match="gitea:alice"):
            sessions._load_config()

    def test_a_failed_load_is_kept_briefly_so_requests_dont_each_retry_it(self, monkeypatch):
        calls = []

        def broken():
            calls.append(1)
            raise sessions.AuthConfigError("vault is down")

        monkeypatch.setattr(sessions, "_config", None)
        monkeypatch.setattr(sessions, "_load_config", broken)
        for _ in range(3):
            with pytest.raises(sessions.AuthConfigError, match="vault is down"):
                sessions.config()
        assert len(calls) == 1


class FakeVault:
    """Stands in for app.utils.vault_client.VaultClient."""
    secrets = {("ui/gitea", "client_id"): "vault-id", ("ui/gitea", "client_secret"): "vault-secret\n"}
    made: list[dict] = []

    def __init__(self, **kwargs):
        FakeVault.made.append(kwargs)

    def get_secret(self, path, key):
        if (path, key) not in self.secrets:
            raise ValueError(f"Key '{key}' not found in secret at {path}")
        return self.secrets[(path, key)]


class TestSecretManagerRefs:
    @pytest.fixture(autouse=True)
    def bundle(self, monkeypatch):
        import app.utils.vault_client as vault_client

        FakeVault.made = []
        monkeypatch.setattr(vault_client, "VaultClient", FakeVault)
        bundle = {
            "platforms": [{"gitea": {"endpoint": "http://gitea:3000"}}],
            "secret_managers": [{"vault": {"endpoint": "http://vault:8200", "mount_point": "kv", "kv_version": 2,
                                           "token_auth": {"token": "s.root"}, "timeout": "5s"}}],
            "ui": {"auth": {"providers": [{
                "platform": "gitea",
                "client_id": {"ref": {"path": "ui/gitea", "key": "client_id"}},
                "client_secret": {"ref": {"path": "ui/gitea", "key": "client_secret"}},
            }]}},
        }
        monkeypatch.setattr(sessions, "_cue_bundle", lambda: bundle)
        return bundle

    def test_references_are_read_from_the_only_secret_manager(self):
        provider = sessions._load_config().platforms["gitea"]
        assert (provider.client_id, provider.client_secret) == ("vault-id", "vault-secret")
        assert FakeVault.made == [{  # one connection for every reference
            "endpoint": "http://vault:8200", "mount_point": "kv", "kv_version": 2, "namespace": None,
            "auth": {"token": "s.root"}, "verify_ssl": True, "ssl_ca": None, "timeout": 5, "max_retries": 3}]

    def test_the_vault_login_can_come_from_a_file(self, bundle, tmp_path):
        (tmp_path / "role-secret").write_text("secret-id\n")
        bundle["secret_managers"] = [{"vault": {"endpoint": "http://vault:8200", "app_role_auth": {
            "role_id": "ui", "secret_id": {"file": str(tmp_path / "role-secret")}, "mount_path": "approle-ui"}}}]
        sessions._load_config()
        assert FakeVault.made[0]["auth"] == {"app_role": {"role_id": "ui", "secret_id": "secret-id", "mount_path": "approle-ui"}}

    def test_the_named_secret_manager_is_used(self, bundle):
        bundle["secret_managers"].append({"vault": {"tag": "prod", "endpoint": "https://vault.prod:8200",
                                                    "userpass_auth": {"username": "vt", "password": "pw"}}})
        bundle["ui"]["auth"]["secret_manager"] = "vault-prod"
        sessions._load_config()
        assert FakeVault.made[0]["endpoint"] == "https://vault.prod:8200"
        assert FakeVault.made[0]["auth"] == {"userpass": {"username": "vt", "password": "pw"}}

    def test_several_secret_managers_need_one_named(self, bundle):
        bundle["secret_managers"].append({"vault": {"tag": "prod", "endpoint": "https://vault.prod:8200",
                                                    "token_auth": {"token": "t"}}})
        with pytest.raises(sessions.AuthConfigError, match="vault, vault-prod"):
            sessions._load_config()

    def test_an_unknown_secret_manager_is_an_error(self, bundle):
        bundle["ui"]["auth"]["secret_manager"] = "vault-staging"
        with pytest.raises(sessions.AuthConfigError, match="'vault-staging' is not in secret_managers"):
            sessions._load_config()

    def test_a_secret_that_cannot_be_read_keeps_the_ui_closed_and_says_where(self, bundle):
        bundle["ui"]["auth"]["providers"][0]["client_secret"] = {"ref": {"path": "ui/gitea", "key": "typo"}}
        with pytest.raises(sessions.AuthConfigError) as caught:
            sessions._load_config()
        message = str(caught.value)
        assert "providers[gitea].client_secret" in message and "ui/gitea#typo" in message
        assert "s.root" not in message and "vault-id" not in message


# ── sessions ──────────────────────────────────────────────────────────────────

class TestSessions:
    def test_the_token_is_stored_encrypted_and_keyed_by_a_hash(self, cfg):
        sid, _ = _session(cfg, "gitea", "tok-alice")
        (key, blob), = ledger._redis().kv.items()
        assert sid not in key and "tok-alice" not in blob
        assert sessions.get_session(cfg, sid)["identities"]["gitea"]["accessToken"] == "tok-alice"

    def test_a_tampered_or_foreign_session_is_refused(self, cfg):
        sid, _ = _session(cfg, "gitea", "tok-alice")
        key = next(iter(ledger._redis().kv))
        ledger._redis().kv[key] = ledger._redis().kv[key][:-4] + "AAAA"
        assert sessions.get_session(cfg, sid) is None
        other = sessions.AuthConfig(**{**cfg.__dict__, "key": sessions._derive_key("other")})
        sid2, _ = _session(cfg, "gitea", "tok-alice")
        assert sessions.get_session(other, sid2) is None

    def test_a_session_from_before_several_platforms_still_works(self, cfg):
        sid = "old-session"
        ledger._redis().set(sessions._session_key(sid), sessions._seal(cfg.key, {
            "user": {"login": "alice"}, "accessToken": "tok-alice", "refreshToken": "", "platform": "gitea",
            "startedAt": "2026-10-12T00:00:00Z"}))
        session = sessions.get_session(cfg, sid)
        assert session["primary"] == "gitea" and session["identities"]["gitea"]["accessToken"] == "tok-alice"

    def test_accounts_on_platforms_no_longer_configured_are_dropped(self, cfg):
        sid, _ = _session(cfg, "gitea", "tok-alice")
        only_github = sessions.AuthConfig(**{**cfg.__dict__, "platforms": {"github": cfg.platforms["github"]}})
        assert sessions.get_session(only_github, sid) is None

    def test_login_state_is_single_use_and_names_the_platform(self, cfg):
        state, _ = sessions.start_login("github", "/rules")
        first = sessions.finish_login(state)
        assert (first["platform"], first["returnTo"]) == ("github", "/rules")
        assert sessions.finish_login(state) is None

    @pytest.mark.parametrize("path,expected", [
        ("/rules/gitea/mongo?env=production", "/rules/gitea/mongo?env=production"),
        ("https://evil.example", "/"), ("//evil.example", "/"), ("/\\evil.example", "/"),
        ("/http://evil.example", "/"), ("", "/"), (None, "/"),
    ])
    def test_return_path_never_leaves_the_ui(self, path, expected):
        assert sessions.safe_return_path(path) == expected


# ── who sees what ─────────────────────────────────────────────────────────────

class TestAccess:
    def test_no_sign_in_means_everyone_sees_everything(self, monkeypatch):
        monkeypatch.setattr(sessions, "config", lambda: None)
        viewer = access.viewer(None)
        assert len([1 for _, p in viewer.visible_rules(RULES, []) if p.write]) == len(RULES)

    def test_sign_in_on_and_no_session_is_refused(self, cfg):
        with pytest.raises(access.Unauthenticated):
            access.viewer(None)
        with pytest.raises(access.Unauthenticated):
            access.viewer("forged")

    def test_rules_follow_the_users_repositories(self, cfg, gitea):
        gitea.access = {"acme/configs": WRITE, "acme/web": READ, "acme/secrets": WRITE}
        _, viewer = _session(cfg, "gitea", "tok-alice")
        assert _names(viewer.visible_rules(RULES, [])) == {"mongo": WRITE, "redis": WRITE}
        # payments/billing is not hers; zookeeper is on GitHub, which she has not connected.

    def test_each_rule_is_judged_on_its_own_platform(self, cfg, gitea, github):
        # The same repository name on two platforms: Gitea says yes, GitHub says no.
        gitea.access = {"acme/configs": READ}
        github.access = {}
        sid, _ = _session(cfg, "gitea", "tok-alice")
        session = sessions.get_session(cfg, sid)
        sessions.connect_identity(cfg, sid, session, platform="github",
                                  user=github.user("tok-alice-gh"), token={"access_token": "tok-alice-gh"})
        viewer = access.viewer(sid)
        assert set(_names(viewer.visible_rules(RULES, []))) == {"mongo", "redis"}
        github.access = {"acme/configs": WRITE}
        access.forget(sid)
        assert _names(access.viewer(sid).visible_rules(RULES, []))["zookeeper"] == WRITE
        # Bitbucket is not a sign-in platform: nobody can vouch for its rule.
        assert "vercel" not in _names(access.viewer(sid).visible_rules(RULES, []))

    def test_excluded_repositories_do_not_count(self, cfg, gitea):
        gitea.access = {"acme/secrets": WRITE}
        _, viewer = _session(cfg, "gitea", "tok-alice")
        assert _names(viewer.visible_rules(RULES, [])) == {}

    def test_an_environment_follows_the_repository_it_was_synced_from(self, cfg, gitea):
        gitea.access = {"acme/web": READ}
        _, viewer = _session(cfg, "gitea", "tok-alice")
        assert viewer.can_see_target(RULES[1], {"datasource": "redis", "repository": "acme/web"})
        assert not viewer.can_see_target(RULES[1], {"datasource": "redis", "repository": "acme/payroll"})

    def test_admins_see_everything(self, cfg, gitea, github):
        _, viewer = _session(cfg, "gitea", "tok-root", login="root")
        assert len(viewer.visible_rules(RULES, [])) == len(RULES)
        assert gitea.calls == github.calls == []

    def test_an_admin_login_on_another_platform_is_not_an_admin(self, cfg, github):
        _, viewer = _session(cfg, "github", "tok-alice-gh", login="root")  # github:root, not gitea:root
        assert not viewer.is_admin

    def test_answers_are_cached_per_session(self, cfg, gitea):
        gitea.access = {"acme/configs": READ}
        _, viewer = _session(cfg, "gitea", "tok-alice")
        viewer.visible_rules(RULES, [])
        before = len(gitea.calls)
        viewer.visible_rules(RULES, [])
        assert len(gitea.calls) == before

    def test_an_expired_token_is_refreshed_once(self, cfg, gitea):
        gitea.access = {"acme/configs": READ}
        sid, viewer = _session(cfg, "gitea", "tok-expired")
        assert viewer.rule_permission(RULES[0]).read
        assert sessions.get_session(cfg, sid)["identities"]["gitea"]["accessToken"] == "tok-alice-new"


# ── the HTTP API ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(cfg, gitea, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.app import create_app
    from app.api.routers import ui
    from app.ui import views

    records = [
        {"tenant": "acme", "datasource": "mongo", "env": "production", "repository": "acme/configs", "desired": {},
         "lastSync": {"taskId": "t1"}},
        {"tenant": "acme", "datasource": "s3", "env": "production", "repository": "payments/billing", "desired": {},
         "lastSync": {"taskId": "t2"}},
    ]
    tasks = [{"taskId": "t1", "rule": "gitea/mongo", "state": "SUCCEEDED", "tenant": "acme"},
             {"taskId": "t2", "rule": "gitea/s3", "state": "SUCCEEDED", "tenant": "acme"}]
    monkeypatch.setattr(ui, "_rules", lambda tenant: RULES)
    monkeypatch.setattr(ui, "_tenant", lambda request: "acme")
    monkeypatch.setattr(ledger, "list_targets", lambda tenant: records)
    monkeypatch.setattr(ledger, "list_tasks", lambda tenant, rule=None, limit=100: [t for t in tasks if not rule or t["rule"] == rule])
    monkeypatch.setattr(ledger, "get_task", lambda tid: next((t for t in tasks if t["taskId"] == tid), None))
    monkeypatch.setattr(ledger, "list_events", lambda tenant, rule=None, limit=200: [
        {"rule": "gitea/mongo", "reason": "SyncSucceeded"}, {"rule": "gitea/s3", "reason": "SyncSucceeded"}])
    monkeypatch.setattr(views, "compare_all", lambda *a: None)
    monkeypatch.setattr(views, "rule_view", lambda rule, tenant, recs, tks: {
        "name": views.rule_name(rule), "envs": sorted(r["env"] for r in recs if r["datasource"] == rule["datasource"])})
    gitea.access = {"acme/configs": READ}
    return TestClient(create_app())


def _signed_in(client, cfg):
    sid, _ = _session(cfg, "gitea", "tok-alice")
    client.cookies.set(sessions.SESSION_COOKIE, sid)
    return client


def _sign_in_with(client, platform, return_to="/"):
    login = client.get("/v1/auth/login", params={"platform": platform, "return_to": return_to}, follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    return client.get("/v1/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)


class TestApi:
    def test_signed_out_requests_are_refused_and_every_platform_is_offered(self, client):
        assert client.get("/v1/ui/rules").status_code == 401
        # Signed out: the platforms to offer, and nothing about the rules or where the platforms are.
        assert client.get("/v1/auth/session").json() == {
            "enabled": True, "authenticated": False,
            "platforms": [{"platform": "gitea", "provider": "gitea"}, {"platform": "github", "provider": "github"}]}

    def test_the_rules_list_shows_only_what_the_user_can_read(self, client, cfg):
        body = _signed_in(client, cfg).get("/v1/ui/rules").json()
        # acme/configs (readable) is named by mongo and matches redis's acme/*; s3's payments/billing is not hers.
        assert body == {"rules": [{"name": "gitea/mongo", "envs": ["production"], "canSync": False},
                                  {"name": "gitea/redis", "envs": [], "canSync": False}]}

    def test_connecting_a_second_platform_adds_its_rules_to_the_same_session(self, client, github):
        github.access = {"acme/configs": WRITE}
        first = _sign_in_with(client, "gitea")
        sid = first.cookies.get(sessions.SESSION_COOKIE)
        assert [r["name"] for r in client.get("/v1/ui/rules").json()["rules"]] == ["gitea/mongo", "gitea/redis"]

        second = _sign_in_with(client, "github", "/rules")
        assert second.headers["location"] == "/rules"
        assert sessions.SESSION_COOKIE not in second.cookies  # same session, no new cookie
        session = client.get("/v1/auth/session").json()
        assert session["user"]["login"] == "alice" and session["primary"] == "gitea"
        assert {p["platform"]: p.get("user", {}).get("login") for p in session["platforms"]} == {
            "gitea": "alice", "github": "alice-gh"}
        rules = {r["name"]: r["canSync"] for r in client.get("/v1/ui/rules").json()["rules"]}
        assert rules == {"gitea/mongo": False, "gitea/redis": False, "github/zookeeper": True}
        assert client.cookies.get(sessions.SESSION_COOKIE) == sid

    def test_login_with_an_unknown_platform_is_refused(self, client):
        resp = client.get("/v1/auth/login", params={"platform": "bitbucket"}, follow_redirects=False)
        assert resp.headers["location"] == "/login?error=platform"

    def test_a_hidden_rule_looks_exactly_like_a_missing_one(self, client, cfg):
        c = _signed_in(client, cfg)
        hidden = c.get("/v1/ui/rules/gitea/s3")
        missing = c.get("/v1/ui/rules/gitea/nope")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json()["detail"].replace("s3", "X") == missing.json()["detail"].replace("nope", "X")

    def test_tasks_and_events_of_hidden_rules_are_left_out(self, client, cfg):
        c = _signed_in(client, cfg)
        assert [t["taskId"] for t in c.get("/v1/ui/tasks").json()["tasks"]] == ["t1"]
        assert [e["rule"] for e in c.get("/v1/ui/events").json()["events"]] == ["gitea/mongo"]
        assert c.get("/v1/ui/tasks/t2").status_code == 404
        assert c.get("/v1/ui/tasks/t1").status_code == 200

    def test_syncing_needs_write_access(self, client, cfg):
        resp = _signed_in(client, cfg).post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": False})
        assert resp.status_code == 403

    def test_login_redirects_to_the_platform_and_binds_the_state_to_the_browser(self, client):
        resp = client.get("/v1/auth/login", params={"platform": "gitea", "return_to": "/rules"}, follow_redirects=False)
        assert resp.status_code == 303
        target = urlparse(resp.headers["location"])
        state = parse_qs(target.query)["state"][0]
        assert target.netloc == "localhost:3000"
        assert resp.cookies.get(sessions.STATE_COOKIE) == state

    def test_callback_signs_in_and_returns_to_the_page(self, client, gitea):
        resp = _sign_in_with(client, "gitea", "/rules/gitea/mongo")
        assert (resp.status_code, resp.headers["location"]) == (303, "/rules/gitea/mongo")
        assert resp.cookies.get(sessions.SESSION_COOKIE)
        assert gitea.verifier  # PKCE verifier sent with the code
        assert client.get("/v1/auth/session").json()["user"]["login"] == "alice"

    def test_a_callback_can_be_used_once_only(self, client):
        login = client.get("/v1/auth/login", params={"platform": "gitea"}, follow_redirects=False)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        client.get("/v1/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)
        client.cookies.set(sessions.STATE_COOKIE, state)
        again = client.get("/v1/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)
        assert again.headers["location"] == "/login?error=expired"

    def test_callback_with_someone_elses_state_is_refused(self, client):
        login = client.get("/v1/auth/login", params={"platform": "gitea"}, follow_redirects=False)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        client.cookies.clear()  # another browser
        resp = client.get("/v1/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)
        assert resp.headers["location"] == "/login?error=state"

    def test_logout_ends_the_session_for_every_platform(self, client, cfg):
        c = _signed_in(client, cfg)
        sid = c.cookies.get(sessions.SESSION_COOKIE)
        assert c.post("/v1/auth/logout").status_code == 204
        assert sessions.get_session(cfg, sid) is None  # gone server-side, not just from the browser
        c.cookies.set(sessions.SESSION_COOKIE, sid)  # a copied cookie no longer works
        assert c.get("/v1/ui/rules").status_code == 401
