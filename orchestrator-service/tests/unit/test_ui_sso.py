"""
tests/unit/test_ui_sso.py

Single sign-on for the web UI with the Git platforms, and what each signed-in
user may see. Signing in only says who someone is; what they may see and do is
the gateway's permission engine's to say (decided and tested in the gateway), so
here a fake engine stands in for it and the tests check that the UI asks it the
right questions with the right tokens, and does what the answer says.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

import app.tasks.etl  # noqa: F401 — the UI router imports it lazily
from app import authz_client
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
    """A platform the user signs in with: it knows who a token belongs to, nothing more."""

    def __init__(self, kind, public_url, login):
        self.kind = kind
        super().__init__(endpoint=public_url, public_url=public_url, client_id=f"{kind}-id", client_secret=f"{kind}-secret")
        self.login = login
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


class FakeEngine:
    """The gateway's permission engine as the web UI meets it. Its answers are set by
    the test, its questions are recorded. The decisions themselves (Git permission, branch
    protection, the policy, admins) are the engine's, tested in the gateway; what is
    tested here is that the UI asks, with the right tokens, and obeys."""

    def __init__(self):
        self.valid: set[str] = set()           # tokens the platforms accept
        self.admins: set[str] = set()          # tokens of admins
        self.grants: dict[tuple, authz_client.RuleAccess] = {}   # (token, rule) → what it may do
        self.sources: dict[tuple, bool] = {}   # (token, repository) → an environment from it is visible
        self.refused: dict[tuple, str] = {}    # (rule, env, action) → the reason a sync is refused
        self.branches: dict[tuple, str] = {}   # (rule, env) → the branch the engine judged
        self.unavailable = False
        self.calls: list[tuple] = []
        self.forgotten: list[list] = []

    @staticmethod
    def key(rule: dict) -> tuple:
        return (rule.get("platform", ""), rule.get("datasource", ""), tuple(rule.get("repositories") or ()))

    def grant(self, token, rule, permission="read", can_sync=False):
        self.valid.add(token)
        self.grants[(token, self.key(rule))] = authz_client.RuleAccess(permission, can_sync)

    def _check(self, identities):
        if self.unavailable:
            raise authz_client.Unavailable("the permission engine is unreachable")
        for _, token in identities:
            if token not in self.valid:
                raise authz_client.TokenRejected("the Git platform rejected the token")

    def _admin(self, identities):
        return any(token in self.admins for _, token in identities)

    def _grant_for(self, identities, rule):
        for platform, token in identities:
            if platform == rule.get("platform") and (token, self.key(rule)) in self.grants:
                return self.grants[(token, self.key(rule))]
        return None

    # the authz_client.Client interface
    def identify(self, identity):
        self.calls.append(("identify", identity))
        self._check([identity])
        platform, token = identity
        return authz_client.Who(platform=platform, login=f"user-{token}", admin=token in self.admins)

    def readable_rules(self, identities, rules, envs=None, tenant=""):
        self.calls.append(("readable_rules", list(identities), [self.key(r) for r in rules], tenant))
        self._check(identities)
        admin = self._admin(identities)
        found = {}
        for i, rule in enumerate(rules):
            access_ = authz_client.RuleAccess("admin", True) if admin else self._grant_for(identities, rule)
            if access_ is not None:
                found[i] = access_
        return admin, found

    def decide(self, identities, action, *, rule=None, envs=(), env="", source_repo="", tenant="", file_path="", via="ui"):
        self.calls.append(("decide", action, env, source_repo, list(identities)))
        self._check(identities)
        admin = self._admin(identities)
        if action == "read":
            ok = admin or any(self.sources.get((token, source_repo), False) for _, token in identities)
            return authz_client.Decision(allowed=ok, reason="" if ok else "permission denied: cannot read the source")
        granted = self._grant_for(identities, rule)
        if admin:
            ok = True
        elif action == "sync":
            ok = bool(granted and granted.can_sync)
        else:
            ok = granted is not None
        reason = self.refused.get((self.key(rule), env, action), "")
        if reason:
            ok = False
        return authz_client.Decision(
            allowed=ok, reason="" if ok else (reason or "permission denied: nothing was granted"),
            branch=self.branches.get((self.key(rule), env), "main"), admin=admin)

    def forget(self, identities):
        self.forgotten.append(list(identities))


@pytest.fixture
def gitea():
    return FakeProvider("gitea", "http://localhost:3000", "alice")


@pytest.fixture
def github():
    return FakeProvider("github", "https://github.com", "alice-gh")


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    authz_client.set_client(fake)
    yield fake
    authz_client.set_client(None)


@pytest.fixture
def cfg(gitea, github, engine, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ledger, "_redis", lambda: fake)
    conf = sessions.AuthConfig(platforms={"gitea": gitea, "github": github},
                               redirect_uri="http://ui.test/api/orchestrator/v1/auth/callback",
                               secure_cookies=False, session_ttl=3600,
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
    {"platform": "bitbucket", "datasource": "helm", "repositories": ["acme/configs"]},
]
MONGO, REDIS, S3, ZOOKEEPER, HELM = RULES


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

    def test_the_provider_only_identifies_users(self):
        # What a user may do is the permission engine's to say, not the sign-in provider's.
        for gone in ("permission", "repos", "can_push"):
            assert not hasattr(scm.Provider, gone)


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
        assert cfg.session_ttl == 3600
        assert not hasattr(cfg, "admins")  # the gateway's permission engine reads ui.auth.admins

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
        cfg = sessions._load_config()
        assert list(cfg.platforms) == ["gitea", "github-enterprise"]
        assert cfg.platforms["gitea"].client_id == "g-id" and cfg.platforms["gitea"].public_url == "http://localhost:3000"
        assert cfg.platforms["github-enterprise"].kind == "github"

    def test_one_platform_keeps_the_short_settings(self, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORM", "gitea")
        monkeypatch.setenv("UI_AUTH_CLIENT_ID", "id")
        monkeypatch.setenv("UI_AUTH_CLIENT_SECRET", "secret")
        cfg = sessions._load_config()
        assert list(cfg.platforms) == ["gitea"]
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

    def test_the_old_admin_variable_is_refused_rather_than_ignored(self, monkeypatch):
        monkeypatch.setenv("UI_AUTH_PLATFORM", "gitea")
        monkeypatch.setenv("UI_AUTH_CLIENT_ID", "id")
        monkeypatch.setenv("UI_AUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("UI_AUTH_ADMINS", "gitea:root")
        with pytest.raises(sessions.AuthConfigError, match="ui.auth.admins"):
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
            "auth": {"token": "s.root"}, "insecure_skip_verify": False, "ssl_ca": None, "ssl_ca_file": None,
            "timeout": 5, "max_retries": 3}]

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
    def test_no_sign_in_means_everyone_sees_everything(self, monkeypatch, engine):
        monkeypatch.setattr(sessions, "config", lambda: None)
        viewer = access.viewer(None)
        assert len([1 for _, p in viewer.visible_rules(RULES, []) if p.can_sync]) == len(RULES)
        assert engine.calls == []  # nobody signed in: nothing to ask about

    def test_sign_in_on_and_no_session_is_refused(self, cfg):
        with pytest.raises(access.Unauthenticated):
            access.viewer(None)
        with pytest.raises(access.Unauthenticated):
            access.viewer("forged")

    def test_the_rules_shown_are_the_ones_the_engine_says_may_be_read(self, cfg, engine):
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.grant("tok-alice", REDIS, "read")
        _, viewer = _session(cfg, "gitea", "tok-alice")
        shown = _names(viewer.visible_rules(RULES, []))
        assert shown == {"mongo": access.Access(True, True, "write"), "redis": access.Access(True, False, "read")}

    def test_one_question_carries_every_connected_account_and_the_tenant(self, cfg, gitea, github, engine):
        engine.grant("tok-alice", MONGO)
        engine.grant("tok-alice-gh", ZOOKEEPER, "write", can_sync=True)
        sid, _ = _session(cfg, "gitea", "tok-alice")
        sessions.connect_identity(cfg, sid, sessions.get_session(cfg, sid), platform="github",
                                  user=github.user("tok-alice-gh"), token={"access_token": "tok-alice-gh"})
        shown = _names(access.viewer(sid).visible_rules(RULES, [], "acme"))
        assert set(shown) == {"mongo", "zookeeper"}  # bitbucket is no sign-in platform: nobody vouches for its rule
        (kind, identities, rules, tenant), = [c for c in engine.calls if c[0] == "readable_rules"]
        assert dict(identities) == {"gitea": "tok-alice", "github": "tok-alice-gh"}
        assert len(rules) == len(RULES) and tenant == "acme"

    def test_the_rules_environments_are_part_of_the_question(self, cfg, engine, monkeypatch):
        seen = {}
        monkeypatch.setattr(engine, "readable_rules", lambda ids, rules, envs=None, tenant="": seen.update(envs=envs) or (False, {}))
        _, viewer = _session(cfg, "gitea", "tok-alice")
        viewer.visible_rules([MONGO, REDIS], [
            {"datasource": "mongo", "env": "production"}, {"datasource": "mongo", "env": "staging"},
            {"datasource": "redis", "env": "qa"}, {"datasource": "other", "env": "x"}])
        assert seen["envs"] == [["production", "staging"], ["qa"]]

    def test_an_environment_follows_the_repository_it_was_synced_from(self, cfg, engine):
        engine.valid.add("tok-alice")
        engine.sources[("tok-alice", "acme/web")] = True
        _, viewer = _session(cfg, "gitea", "tok-alice")
        assert viewer.can_see_target(REDIS, {"datasource": "redis", "repository": "acme/web"})
        assert not viewer.can_see_target(REDIS, {"datasource": "redis", "repository": "acme/payroll"})
        asked = [c for c in engine.calls if c[0] == "decide"]
        assert [(c[1], c[3]) for c in asked] == [("read", "acme/web"), ("read", "acme/payroll")]

    def test_an_environment_with_no_recorded_source_is_as_visible_as_its_rule(self, cfg, engine):
        engine.valid.add("tok-alice")
        _, viewer = _session(cfg, "gitea", "tok-alice")
        assert viewer.can_see_target(REDIS, {"datasource": "redis"})
        assert engine.calls == []

    def test_a_repository_is_asked_about_once_however_many_environments_came_from_it(self, cfg, engine):
        engine.valid.add("tok-alice")
        engine.sources[("tok-alice", "acme/web")] = True
        _, viewer = _session(cfg, "gitea", "tok-alice")
        for env in ("a", "b", "c"):
            assert viewer.can_see_target(REDIS, {"datasource": "redis", "env": env, "repository": "acme/web"})
        assert len([c for c in engine.calls if c[0] == "decide"]) == 1

    def test_admins_are_who_the_engine_says(self, cfg, engine):
        engine.admins.add("tok-root")
        engine.valid.update({"tok-root", "tok-alice"})
        _, root = _session(cfg, "gitea", "tok-root", login="root")
        _, alice = _session(cfg, "gitea", "tok-alice")
        assert root.is_admin and not alice.is_admin
        assert len(root.visible_rules(RULES, [])) == len(RULES)

    def test_a_sync_is_judged_by_the_engine_per_environment(self, cfg, engine):
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.branches[(engine.key(MONGO), "production")] = "release"
        _, viewer = _session(cfg, "gitea", "tok-alice")
        decision = viewer.decide(MONGO, "sync", "production", "acme")
        assert decision.allowed and decision.branch == "release"
        assert [c[:3] for c in engine.calls if c[0] == "decide"] == [("decide", "sync", "production")]

    def test_an_open_ui_judges_nothing_and_reads_the_branch_the_rule_names(self, monkeypatch, engine):
        monkeypatch.setattr(sessions, "config", lambda: None)
        decision = access.viewer(None).decide({"branch_map": {"main": "production"}}, "sync", "production")
        assert decision.allowed and decision.branch == "main"
        assert engine.calls == []

    def test_an_expired_token_is_refreshed_once(self, cfg, engine):
        engine.grant("tok-alice-new", MONGO)
        sid, viewer = _session(cfg, "gitea", "tok-expired")
        assert viewer.rule_access(MONGO).read
        assert sessions.get_session(cfg, sid)["identities"]["gitea"]["accessToken"] == "tok-alice-new"

    def test_a_token_that_cannot_be_refreshed_ends_the_session(self, cfg, engine):
        user = scm.ScmUser(login="alice", name="alice", email="", avatar_url="", is_admin=False)
        sid = sessions.create_session(cfg, platform="gitea", user=user, token={"access_token": "tok-expired"})
        with pytest.raises(access.Unauthenticated):
            access.viewer(sid).rule_access(MONGO)

    def test_a_token_still_rejected_after_a_refresh_ends_the_session(self, cfg, engine):
        # The platform accepts neither the old token nor the refreshed one.
        _, viewer = _session(cfg, "gitea", "tok-expired")
        with pytest.raises(access.Unauthenticated):
            viewer.rule_access(MONGO)

    def test_an_engine_that_cannot_be_reached_is_not_replaced_by_a_guess(self, cfg, engine):
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.unavailable = True
        _, viewer = _session(cfg, "gitea", "tok-alice")
        with pytest.raises(authz_client.Unavailable):
            viewer.visible_rules(RULES, [])
        with pytest.raises(authz_client.Unavailable):
            viewer.decide(MONGO, "sync", "production")

    def test_without_an_engine_address_sign_in_cannot_judge_anyone(self, cfg, monkeypatch):
        authz_client.set_client(None)
        from app.config import settings
        monkeypatch.setattr(settings, "GATEWAY_AUTHZ_ADDR", "")
        _, viewer = _session(cfg, "gitea", "tok-alice")
        with pytest.raises(authz_client.Unavailable, match="GATEWAY_AUTHZ_ADDR"):
            viewer.visible_rules(RULES, [])

    def test_signing_out_asks_the_engine_to_forget_the_sessions_tokens(self, cfg, github, engine):
        sid, _ = _session(cfg, "gitea", "tok-alice")
        access.forget(sid, sessions.get_session(cfg, sid))
        assert engine.forgotten == [[("gitea", "tok-alice")]]


# ── the HTTP API ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(cfg, gitea, engine, monkeypatch):
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
    monkeypatch.setattr(ui, "_tenants", lambda request, tenant=None: ["acme"])
    monkeypatch.setattr(ledger, "list_targets", lambda tenant: records)
    monkeypatch.setattr(ledger, "list_tasks", lambda tenant, rule=None, rules=None, limit=100: [
        t for t in tasks if (not rule or t["rule"] == rule) and (rules is None or t["rule"] in rules)][:limit])
    monkeypatch.setattr(ledger, "get_task", lambda tid: next((t for t in tasks if t["taskId"] == tid), None))
    monkeypatch.setattr(ledger, "list_events", lambda tenant, rule=None, rules=None, limit=200: [
        e for e in [{"rule": "gitea/mongo", "reason": "SyncSucceeded"}, {"rule": "gitea/s3", "reason": "SyncSucceeded"}]
        if rules is None or e["rule"] in rules][:limit])
    monkeypatch.setattr(views, "compare_all", lambda *a: None)
    monkeypatch.setattr(views, "rule_view", lambda rule, tenant, recs, tks: {
        "name": views.rule_name(rule), "envs": sorted(r["env"] for r in recs if r["datasource"] == rule["datasource"])})
    # alice reads what acme/configs feeds: mongo, and redis (acme/*); s3 is not hers.
    engine.grant("tok-alice", MONGO)
    engine.grant("tok-alice", REDIS)
    engine.sources[("tok-alice", "acme/configs")] = True  # the environment synced from it is hers to see
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
        assert body["rules"] == [{"name": "gitea/mongo", "envs": ["production"], "canSync": False},
                                 {"name": "gitea/redis", "envs": [], "canSync": False}]
        # github holds one more rule; alice has not connected it. gitea's s3 is hers to read or not, and is not counted.
        assert body["hidden"] == [{"platform": "github", "rules": 1}]

    def test_connecting_a_second_platform_adds_its_rules_to_the_same_session(self, client, github, engine):
        engine.grant("tok-alice-gh", ZOOKEEPER, "write", can_sync=True)
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

    def test_a_sync_the_engine_refuses_is_a_403_with_the_engines_reason_and_queues_nothing(self, client, cfg, engine, monkeypatch):
        import app.tasks.etl as etl
        queued = []
        monkeypatch.setattr(etl.process_webhook_task, "apply_async", lambda kwargs, queue: queued.append(kwargs))
        resp = _signed_in(client, cfg).post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": False})
        assert resp.status_code == 403 and resp.json()["detail"] == "permission denied: nothing was granted"
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.refused[(engine.key(MONGO), "production", "sync")] = (
            "permission denied: alice on gitea may not push to main of acme/configs (the branch that feeds production)")
        resp = client.post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": False})
        assert resp.status_code == 403 and "may not push to main" in resp.json()["detail"]
        assert queued == []

    def test_the_engine_being_unreachable_is_a_503_never_a_local_answer(self, client, cfg, engine):
        c = _signed_in(client, cfg)
        engine.unavailable = True
        for path in ("/v1/ui/rules", "/v1/ui/rules/gitea/mongo", "/v1/ui/tasks", "/v1/ui/events", "/v1/ui/bundle"):
            resp = c.get(path)
            assert resp.status_code == 503, path
            assert resp.json() == {"detail": "permissions are unavailable; try again shortly"}
        assert c.post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": True}).status_code == 503

    def test_a_dry_run_is_asked_as_a_dry_run(self, client, cfg, engine, monkeypatch):
        from types import SimpleNamespace
        import app.pipeline.git_extractor as git_extractor
        import app.tasks.etl as etl
        queued = []
        monkeypatch.setattr(git_extractor, "GitExtractor", lambda: SimpleNamespace(get_commit_sha=lambda *a, **k: "abc"))
        monkeypatch.setattr(etl.process_webhook_task, "apply_async",
                            lambda kwargs, queue: queued.append(kwargs) or SimpleNamespace(id="task-1"))
        resp = _signed_in(client, cfg).post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": True})
        assert resp.status_code == 200 and queued[0]["dry_run"] is True
        assert [c[1] for c in engine.calls if c[0] == "decide" and c[1] != "read"] == ["dry_run"]

    def test_a_sync_reads_the_branch_the_engine_judged(self, client, cfg, engine, monkeypatch):
        from types import SimpleNamespace
        import json
        import app.pipeline.git_extractor as git_extractor
        import app.tasks.etl as etl
        read_branches, queued = [], []
        monkeypatch.setattr(git_extractor, "GitExtractor", lambda: SimpleNamespace(
            get_commit_sha=lambda repo_url, branch, token=None, fresh=False: read_branches.append((branch, fresh)) or "abc"))
        monkeypatch.setattr(etl.process_webhook_task, "apply_async",
                            lambda kwargs, queue: queued.append(kwargs) or SimpleNamespace(id="task-1"))
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.branches[(engine.key(MONGO), "production")] = "release"
        resp = _signed_in(client, cfg).post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": False})
        assert resp.status_code == 200
        assert read_branches == [("release", True)]      # fresh: a sync reads the branch as it is now, not a remembered head
        assert json.loads(queued[0]["raw_payload"])["ref"] == "refs/heads/release"

    def test_an_allowed_decision_with_no_branch_is_never_acted_on(self, client, cfg, engine, monkeypatch):
        """The engine refuses an environment nothing feeds (gateway-service/internal/authz: `nothing feeds …`, 403).
        If a decision ever allows one with no branch, nothing is synced and the response says whose bug it is."""
        import app.tasks.etl as etl
        queued = []
        monkeypatch.setattr(etl.process_webhook_task, "apply_async", lambda **kw: queued.append(kw))
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.branches[(engine.key(MONGO), "production")] = ""
        resp = _signed_in(client, cfg).post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": False})
        assert resp.status_code == 500 and "allowed production but named no branch" in resp.json()["detail"]
        assert queued == []

    def test_an_environment_nothing_feeds_is_refused_with_the_engines_reason(self, client, cfg, engine):
        reason = "permission denied: nothing feeds production in the mongo rule: …"
        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.refused[(engine.key(MONGO), "production", "sync")] = reason
        resp = _signed_in(client, cfg).post("/v1/ui/rules/gitea/mongo/sync", json={"envs": [], "dryRun": False})
        assert resp.status_code == 403 and resp.json()["detail"] == reason

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

    def test_logout_ends_the_session_for_every_platform(self, client, cfg, engine):
        c = _signed_in(client, cfg)
        sid = c.cookies.get(sessions.SESSION_COOKIE)
        assert c.post("/v1/auth/logout").status_code == 204
        assert engine.forgotten == [[("gitea", "tok-alice")]]  # and the engine drops what it knew of the token
        assert sessions.get_session(cfg, sid) is None  # gone server-side, not just from the browser
        c.cookies.set(sessions.SESSION_COOKIE, sid)  # a copied cookie no longer works
        assert c.get("/v1/ui/rules").status_code == 401


# ── every tenant at once, as Git allows ───────────────────────────────────────

TENANT_RULES = {
    "acme": [
        {"platform": "gitea", "datasource": "mongo", "repositories": ["acme/configs"]},
        {"platform": "github", "datasource": "zookeeper", "repositories": ["acme/configs"]},
    ],
    "corp": [
        {"platform": "gitea", "datasource": "mongo", "repositories": ["corp/configs"]},  # same name as acme's
        {"platform": "gitea", "datasource": "s3", "repositories": ["payments/billing"]},
        {"platform": "github", "datasource": "zookeeper", "repositories": ["corp/configs"]},
    ],
}


@pytest.fixture
def tenants(cfg, gitea, github, engine, monkeypatch):
    """Two tenants, each with a mongo rule on gitea, served through the real HTTP API."""
    from fastapi.testclient import TestClient
    from app.api.app import create_app
    from app.api.routers import ui
    from app.ui import views

    records = {
        "acme": [{"tenant": "acme", "datasource": "mongo", "env": "production", "repository": "acme/configs",
                  "desired": {}, "ref": "main", "repoUrl": "http://git/acme/configs.git"}],
        "corp": [{"tenant": "corp", "datasource": "mongo", "env": "production", "repository": "corp/configs",
                  "desired": {}, "ref": "main", "repoUrl": "http://git/corp/configs.git"}],
    }
    tasks = {"acme": [{"taskId": "a1", "rule": "gitea/mongo", "state": "SUCCEEDED", "tenant": "acme", "createdAt": "2026-01-02T00:00:00Z"}],
             "corp": [{"taskId": "c1", "rule": "gitea/mongo", "state": "SUCCEEDED", "tenant": "corp", "createdAt": "2026-01-03T00:00:00Z"}]}
    events = {"acme": [{"rule": "gitea/mongo", "reason": "SyncSucceeded", "time": "2026-01-02T00:00:00Z"}],
              "corp": [{"rule": "gitea/mongo", "reason": "SyncSucceeded", "time": "2026-01-03T00:00:00Z"}]}
    monkeypatch.setattr(ui, "_all_tenants", lambda: ["acme", "corp"])
    monkeypatch.setattr(ui, "_rules", lambda tenant: TENANT_RULES[tenant])
    monkeypatch.setattr(ui, "_cue_bundle", lambda: {"platforms": [{"gitea": {"endpoint": "http://git"}}],
                                                    "datasources": [{"mongo": {"tag": ""}}, {"s3": {"tag": ""}}],
                                                    "tenants": [
                                                        {"name": "acme", "schema_repo": {"platform": "gitea", "repo": "acme/schemas", "branch": "main"}},
                                                        {"name": "corp", "schema_repo": {"platform": "gitea", "repo": "corp/schemas"}},
                                                    ]})
    monkeypatch.setattr(ledger, "list_targets", lambda t: records[t])
    monkeypatch.setattr(ledger, "get_target", lambda t, ds, env: next(
        (r for r in records[t] if r["datasource"] == ds and r["env"] == env), None))
    monkeypatch.setattr(ledger, "list_tasks", lambda t, rule=None, rules=None, limit=100: [
        x for x in tasks[t] if (not rule or x["rule"] == rule) and (rules is None or x["rule"] in rules)][:limit])
    monkeypatch.setattr(ledger, "list_events", lambda t, rule=None, rules=None, limit=200: [
        e for e in events[t] if (not rule or e["rule"] == rule) and (rules is None or e["rule"] in rules)][:limit])
    monkeypatch.setattr(views, "compare_all", lambda *a: None)
    monkeypatch.setattr(views, "rule_view", lambda rule, tenant, recs, tks: {"name": views.rule_name(rule), "tenant": tenant})
    for repo in ("acme/configs", "corp/configs"):
        engine.sources[("tok-alice", repo)] = True  # an environment synced from a repository she reads is visible
    return TestClient(create_app())


def _as(client, cfg, token="tok-alice", login="alice", platform="gitea"):
    sid, _ = _session(cfg, platform, token, login=login)
    client.cookies.set(sessions.SESSION_COOKIE, sid)
    return client


def _rule_ids(body):
    return sorted((r["tenant"], r["name"]) for r in body["rules"])


class TestTenants:
    def test_without_sign_in_every_tenants_rules_come_in_one_list(self, tenants, monkeypatch):
        monkeypatch.setattr(sessions, "config", lambda: None)
        assert _rule_ids(tenants.get("/v1/ui/rules").json()) == [
            ("acme", "gitea/mongo"), ("acme", "github/zookeeper"),
            ("corp", "gitea/mongo"), ("corp", "gitea/s3"), ("corp", "github/zookeeper")]

    def test_a_user_sees_the_tenants_whose_rules_the_engine_lets_her_read_and_no_others(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        c = _as(tenants, cfg)
        assert _rule_ids(c.get("/v1/ui/rules").json()) == [("acme", "gitea/mongo")]
        # corp appears nowhere: not in the tasks, the events, nor the tenants the bundle names.
        assert [t["tenant"] for t in c.get("/v1/ui/tasks").json()["tasks"]] == ["acme"]
        assert [e["tenant"] for e in c.get("/v1/ui/events").json()["events"]] == ["acme"]
        assert c.get("/v1/ui/bundle").json()["tenants"] == ["acme"]

    def test_the_bundle_names_every_tenants_schema_repository_without_sign_in(self, tenants, monkeypatch):
        monkeypatch.setattr(sessions, "config", lambda: None)
        assert tenants.get("/v1/ui/bundle").json()["schemaRepos"] == [
            {"tenant": "acme", "platform": "gitea", "repo": "acme/schemas", "branch": "main"},
            {"tenant": "corp", "platform": "gitea", "repo": "corp/schemas", "branch": "main"}]

    def test_the_bundle_names_only_the_schema_repositories_of_tenants_the_user_sees(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        assert _as(tenants, cfg).get("/v1/ui/bundle").json()["schemaRepos"] == [
            {"tenant": "acme", "platform": "gitea", "repo": "acme/schemas", "branch": "main"}]

    def test_the_engine_is_asked_per_tenant_with_that_tenants_rules(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        _as(tenants, cfg).get("/v1/ui/rules")
        asked = {c[3]: c[2] for c in engine.calls if c[0] == "readable_rules"}
        assert asked["acme"] == [engine.key(r) for r in TENANT_RULES["acme"]]
        assert asked["corp"] == [engine.key(r) for r in TENANT_RULES["corp"]]

    def test_a_user_who_reads_both_sees_both_without_choosing_a_tenant(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        engine.grant("tok-alice", TENANT_RULES["corp"][0], "write", can_sync=True)
        body = _as(tenants, cfg).get("/v1/ui/rules").json()
        assert _rule_ids(body) == [("acme", "gitea/mongo"), ("corp", "gitea/mongo")]
        assert {(r["tenant"], r["canSync"]) for r in body["rules"]} == {("acme", False), ("corp", True)}

    def test_an_admin_sees_every_tenant(self, tenants, cfg, gitea, engine):
        gitea.valid_tokens.add("tok-root")
        engine.admins.add("tok-root")
        engine.valid.add("tok-root")
        body = _as(tenants, cfg, token="tok-root", login="root").get("/v1/ui/rules").json()
        assert {r["tenant"] for r in body["rules"]} == {"acme", "corp"}
        assert len(body["rules"]) == 5
        assert "hidden" not in body  # nothing is hidden from an admin

    def test_asking_for_a_tenant_narrows_the_answer_and_never_widens_it(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        c = _as(tenants, cfg)
        assert _rule_ids(c.get("/v1/ui/rules", params={"tenant": "acme"}).json()) == [("acme", "gitea/mongo")]
        assert c.get("/v1/ui/rules", headers={"X-Tenant-ID": "acme"}).json()["rules"][0]["tenant"] == "acme"
        # corp's rule is not hers: naming the tenant does not show it.
        assert c.get("/v1/ui/rules", params={"tenant": "corp"}).json()["rules"] == []

    def test_a_rule_name_in_two_tenants_needs_the_tenant_to_pick_one(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        engine.grant("tok-alice", TENANT_RULES["corp"][0])
        c = _as(tenants, cfg)
        both = c.get("/v1/ui/rules/gitea/mongo")
        assert both.status_code == 409 and "acme, corp" in both.json()["detail"]
        assert c.get("/v1/ui/rules/gitea/mongo", params={"tenant": "corp"}).json()["tenant"] == "corp"

    def test_a_rule_name_only_one_visible_tenant_has_needs_no_tenant(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["corp"][0])
        assert _as(tenants, cfg).get("/v1/ui/rules/gitea/mongo").json()["tenant"] == "corp"

    def test_hidden_counts_rules_on_platforms_not_connected_and_nothing_else(self, tenants, cfg, engine):
        # alice has gitea only. github holds a rule in each tenant (2); corp's gitea/s3 is gitea's to judge, so it is not counted.
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        body = _as(tenants, cfg).get("/v1/ui/rules").json()
        assert body["hidden"] == [{"platform": "github", "rules": 2}]

    def test_the_session_counts_every_tenants_rules_per_platform(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        platforms = {p["platform"]: p["rules"] for p in _as(tenants, cfg).get("/v1/auth/session").json()["platforms"]}
        assert platforms == {"gitea": 3, "github": 2}

    def test_events_and_tasks_of_all_tenants_merge_newest_first_and_name_their_tenant(self, tenants, cfg, engine):
        engine.grant("tok-alice", TENANT_RULES["acme"][0])
        engine.grant("tok-alice", TENANT_RULES["corp"][0])
        c = _as(tenants, cfg)
        assert [(t["taskId"], t["tenant"]) for t in c.get("/v1/ui/tasks").json()["tasks"]] == [("c1", "corp"), ("a1", "acme")]
        assert [e["tenant"] for e in c.get("/v1/ui/events").json()["events"]] == ["corp", "acme"]
        assert [t["taskId"] for t in c.get("/v1/ui/tasks", params={"limit": 1}).json()["tasks"]] == ["c1"]

    def test_a_sync_is_queued_for_the_tenant_the_rule_was_resolved_in(self, tenants, cfg, engine, monkeypatch):
        from types import SimpleNamespace
        import app.pipeline.git_extractor as git_extractor
        import app.tasks.etl as etl

        queued = []
        monkeypatch.setattr(git_extractor, "GitExtractor",
                            lambda: SimpleNamespace(get_commit_sha=lambda repo_url, branch, token=None, fresh=False: "abc123"))
        monkeypatch.setattr(etl.process_webhook_task, "apply_async",
                            lambda kwargs, queue: queued.append(kwargs) or SimpleNamespace(id="task-1"))
        engine.grant("tok-alice", TENANT_RULES["acme"][0], "write", can_sync=True)
        engine.grant("tok-alice", TENANT_RULES["corp"][0], "write", can_sync=True)
        resp = _as(tenants, cfg).post("/v1/ui/rules/gitea/mongo/sync", params={"tenant": "corp"},
                                      json={"envs": [], "dryRun": False})
        assert resp.status_code == 200 and resp.json() == {"taskIds": ["task-1"]}
        assert [(q["tenant_id"], q["datasource"]) for q in queued] == [("corp", "mongo")]


class TestWhatFeedsAnEnvironment:
    """The rule view marks each environment with the branch that feeds it, or why none does. The answer is the
    permission engine's own, so what the UI shows is what a sync would do."""

    RULE = {"platform": "gitea", "datasource": "redis", "branch_map": {"develop": "staging"}}

    def test_each_environment_says_which_branch_feeds_it_or_why_none_does(self):
        from app.api.routers.ui import _say_what_feeds
        from app.ui import access

        targets = [{"env": "staging"}, {"env": "default"}, {"env": "main"}]
        _say_what_feeds(access.Viewer(), self.RULE, "acme", targets)
        assert targets[0] == {"env": "staging", "fedBy": "develop"}
        assert targets[1] == {"env": "default", "fedBy": "main"}           # the rule's own branch feeds default
        assert "fedBy" not in targets[2]
        assert targets[2]["notFed"].startswith("nothing feeds main in the redis rule")

    def test_a_signed_in_viewer_gets_the_engines_answer(self, client, cfg, engine):
        from app.api.routers.ui import _say_what_feeds
        from app.ui import access

        engine.grant("tok-alice", MONGO, "write", can_sync=True)
        engine.branches[(engine.key(MONGO), "production")] = "release"
        _signed_in(client, cfg)
        viewer = access.viewer(client.cookies.get("vt_session"))
        targets = [{"env": "production"}]
        _say_what_feeds(viewer, MONGO, "acme", targets)
        assert targets == [{"env": "production", "fedBy": "release"}]

    def test_unavailable_permissions_leave_the_rule_view_unmarked_not_broken(self):
        from fastapi import HTTPException

        from app.api.routers.ui import _say_what_feeds

        class Down:
            def decide(self, *a, **k):
                raise HTTPException(status_code=503, detail="permissions are unavailable")

        targets = [{"env": "staging"}]
        _say_what_feeds(Down(), self.RULE, "acme", targets)
        assert targets == [{"env": "staging"}]


class TestAvatarLinks:
    """The platform writes its own configured address into an avatar link; the browser needs the address it reaches the platform at."""

    def _gitea(self, endpoint, public_url):
        return scm.Gitea(endpoint=endpoint, public_url=public_url, client_id="id", client_secret="s")

    def test_an_avatar_on_the_platforms_own_address_is_opened_at_the_browsers_address(self):
        user = self._gitea("http://gitea:3000", "http://localhost:3000")._user({"login": "a", "avatar_url": "http://gitea:3000/avatars/abc"})
        assert user.avatar_url == "http://localhost:3000/avatars/abc"

    def test_an_avatar_elsewhere_is_left_alone(self):
        user = self._gitea("http://gitea:3000", "http://localhost:3000")._user({"login": "a", "avatar_url": "https://cdn.example/a.png"})
        assert user.avatar_url == "https://cdn.example/a.png"

    def test_when_the_browser_and_the_orchestrator_share_an_address_nothing_changes(self):
        user = self._gitea("https://git.example", "")._user({"login": "a", "avatar_url": "https://git.example/avatars/abc"})
        assert user.avatar_url == "https://git.example/avatars/abc"


class TestForgetAnEnvironment:
    """An environment no branch feeds any more can be forgotten, by an administrator; one still fed cannot."""

    URL = "/v1/ui/rules/gitea/mongo/targets/{env}"

    def _forgotten(self, monkeypatch):
        from app.ui import ledger
        forgotten = []
        monkeypatch.setattr(ledger, "forget_target", lambda tenant, ds, env: forgotten.append((tenant, ds, env)) or True)
        return forgotten

    def test_without_sign_in_a_leftover_is_forgotten(self, tenants, monkeypatch):
        monkeypatch.setattr(sessions, "config", lambda: None)
        forgotten = self._forgotten(monkeypatch)
        resp = tenants.delete(self.URL.format(env="staging"), params={"tenant": "acme"})
        assert resp.status_code == 204 and forgotten == [("acme", "mongo", "staging")]

    def test_an_environment_a_branch_still_feeds_is_refused(self, tenants, monkeypatch):
        monkeypatch.setattr(sessions, "config", lambda: None)
        forgotten = self._forgotten(monkeypatch)
        resp = tenants.delete(self.URL.format(env="default"), params={"tenant": "acme"})   # the rule's own branch feeds `default`
        assert resp.status_code == 409 and "still fed" in resp.json()["detail"] and forgotten == []

    def test_a_signed_in_person_who_is_not_an_administrator_is_refused(self, tenants, cfg, engine, monkeypatch):
        engine.grant("tok-alice", TENANT_RULES["acme"][0], "write", can_sync=True)
        forgotten = self._forgotten(monkeypatch)
        resp = _as(tenants, cfg).delete(self.URL.format(env="staging"), params={"tenant": "acme"})
        assert resp.status_code == 403 and "administrator" in resp.json()["detail"] and forgotten == []


class TestRefreshReadsTheSinksNow:
    def _scene(self, tenants, monkeypatch):
        from app.ui import ledger, views
        monkeypatch.setattr(sessions, "config", lambda: None)
        record = {"tenant": "acme", "datasource": "mongo", "env": "default", "repository": "acme/configs", "desired": {}, "lastSync": {}}
        monkeypatch.setattr(ledger, "list_targets", lambda t: [record] if t == "acme" else [])
        calls = []
        monkeypatch.setattr(views, "compare", lambda rule, tenant, rec, fresh=False: calls.append(fresh) or {
            "error": None, "keys": [], "differs": 0, "checkedAt": "t"})
        return calls

    def test_fresh_reads_every_fed_environment_now(self, tenants, monkeypatch):
        calls = self._scene(tenants, monkeypatch)
        assert tenants.get("/v1/ui/rules/gitea/mongo", params={"tenant": "acme", "fresh": "true"}).status_code == 200
        assert True in calls          # read now, ahead of building the view

    def test_without_it_the_last_answer_is_good_enough(self, tenants, monkeypatch):
        calls = self._scene(tenants, monkeypatch)
        assert tenants.get("/v1/ui/rules/gitea/mongo", params={"tenant": "acme"}).status_code == 200
        assert True not in calls      # the view is built from the last read

