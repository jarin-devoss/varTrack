"""
tests/unit/test_authz_client.py

The orchestrator's connection to the gateway's permission engine (app/authz_client.py),
against a real gRPC server standing in for the engine: what is sent (the rule's facts,
the tokens, a signed grant), and what each failure means to the caller.
"""
from __future__ import annotations

from concurrent import futures

import pytest

grpc = pytest.importorskip("grpc")
authz_pb2 = pytest.importorskip("vartrack.v1.services.authz_pb2")
authz_pb2_grpc = pytest.importorskip("vartrack.v1.services.authz_pb2_grpc")

from app import authz_client  # noqa: E402
from app.api import tokens  # noqa: E402

SECRET = "an-internal-secret-of-at-least-32-characters"

RULE = {
    "platform": "gitea", "datasource": "mongo", "repositories": ["acme/configs"],
    "exclude_repositories": ["acme/legacy-*"], "branch_map": {"main": "production"}, "branch_as_env": False,
    "overrides": [{"enable": True, "match_repositories": ["acme/extra"], "exclude_repositories": ["acme/x"]}],
}


class FakeAuthz(authz_pb2_grpc.AuthzServicer):
    """Records what it is asked and answers from `answer`."""

    def __init__(self):
        self.seen: dict = {}
        self.fail: grpc.StatusCode | None = None
        self.detail = ""

    def _maybe_fail(self, context):
        self.seen["metadata"] = dict(context.invocation_metadata())
        if self.fail:
            context.abort(self.fail, self.detail)

    def Identify(self, request, context):
        self._maybe_fail(context)
        self.seen["identify"] = request
        return authz_pb2.Who(platform=request.identity.platform, login="alice", admin=True)

    def Decide(self, request, context):
        self._maybe_fail(context)
        self.seen["decide"] = request
        return authz_pb2.Decision(allowed=False, reason="permission denied: no", denied_by="git", login="alice",
                                  platform="gitea", permission="read", branch="main")

    def ReadableRules(self, request, context):
        self._maybe_fail(context)
        self.seen["readable"] = request
        return authz_pb2.ReadableRulesResponse(admin=False, rules=[
            authz_pb2.RulePermission(index=1, permission="write", can_sync=True)])

    def Forget(self, request, context):
        self._maybe_fail(context)
        self.seen["forget"] = request
        from google.protobuf import empty_pb2
        return empty_pb2.Empty()


@pytest.fixture
def engine():
    servicer = FakeAuthz()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    authz_pb2_grpc.add_AuthzServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    client = authz_client.Client(f"127.0.0.1:{port}", timeout=5)
    yield client, servicer
    server.stop(None)


@pytest.fixture(autouse=True)
def open_orchestrator(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "INTERNAL_AUTH_SECRET", "")
    monkeypatch.setattr(settings, "MTLS_ENABLED", False)


def test_a_rule_is_described_by_the_repositories_the_pipeline_checks_payloads_against():
    facts = authz_client.rule_facts(RULE, envs=["staging", "production", "staging"])
    assert (facts.platform, facts.datasource) == ("gitea", "mongo")
    assert list(facts.repositories) == ["acme/configs", "acme/extra"]          # overrides folded in
    assert list(facts.exclude_repositories) == ["acme/legacy-*", "acme/x"]
    assert dict(facts.branch_map) == {"main": "production"} and not facts.branch_as_env
    assert list(facts.envs) == ["production", "staging"]


def test_a_rule_with_no_repositories_stands_for_the_one_its_url_names():
    facts = authz_client.rule_facts({"platform": "gitea", "datasource": "d", "repo_url": "http://git/acme/configs.git"})
    assert list(facts.repositories) == ["acme/configs"]


def test_decide_sends_the_question_and_returns_the_answer(engine):
    client, servicer = engine
    got = client.decide([("gitea", "tok-a"), ("github", "tok-b")], "sync", rule=RULE, envs=["production"],
                        env="production", source_repo="acme/configs", tenant="acme", via="ui")
    sent = servicer.seen["decide"]
    assert [(i.platform, i.token) for i in sent.identities] == [("gitea", "tok-a"), ("github", "tok-b")]
    assert (sent.action, sent.env, sent.source_repo, sent.tenant_id, sent.via) == (
        "sync", "production", "acme/configs", "acme", "ui")
    assert sent.rule.datasource == "mongo" and list(sent.rule.envs) == ["production"]
    assert got == authz_client.Decision(allowed=False, reason="permission denied: no", denied_by="git", login="alice",
                                        platform="gitea", admin=False, permission="read", branch="main")


def test_readable_rules_returns_what_may_be_done_by_position(engine):
    client, servicer = engine
    admin, found = client.readable_rules([("gitea", "tok")], [RULE, {**RULE, "datasource": "redis"}],
                                         [["production"], []], tenant="acme")
    assert admin is False and found == {1: authz_client.RuleAccess("write", True)}
    sent = servicer.seen["readable"]
    assert [r.datasource for r in sent.rules] == ["mongo", "redis"] and sent.tenant_id == "acme"
    assert list(sent.rules[0].envs) == ["production"]


def test_identify_and_forget(engine):
    client, servicer = engine
    assert client.identify(("gitea", "tok")) == authz_client.Who(platform="gitea", login="alice", admin=True)
    client.forget([("gitea", "tok")])
    assert [(i.platform, i.token) for i in servicer.seen["forget"].identities] == [("gitea", "tok")]


def test_every_call_carries_a_signed_authz_grant_when_internal_authentication_is_on(engine, monkeypatch):
    from app.config import settings
    client, servicer = engine
    monkeypatch.setattr(settings, "INTERNAL_AUTH_SECRET", SECRET)
    client.identify(("gitea", "tok"))
    bearer = servicer.seen["metadata"]["authorization"]
    grant = tokens.verify(tokens.bearer(bearer), tokens.AUDIENCE_AUTHZ)
    assert grant.subject == "orchestrator"
    # An authz grant opens nothing else, and no other grant opens this.
    with pytest.raises(tokens.GrantError, match="wrong audience"):
        tokens.verify(tokens.bearer(bearer), tokens.AUDIENCE_SYNC)


def test_no_grant_is_sent_while_the_orchestrator_runs_open(engine):
    client, servicer = engine
    client.identify(("gitea", "tok"))
    assert "authorization" not in servicer.seen["metadata"]


def test_the_authz_key_may_differ_from_the_main_one(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "INTERNAL_AUTH_SECRET", SECRET)
    monkeypatch.setattr(settings, "INTERNAL_AUTH_AUTHZ_SECRET", "")
    assert tokens.key_for(tokens.AUDIENCE_AUTHZ) == SECRET
    monkeypatch.setattr(settings, "INTERNAL_AUTH_AUTHZ_SECRET", "x" * 40)
    assert tokens.key_for(tokens.AUDIENCE_AUTHZ) == "x" * 40
    assert tokens.key_for(tokens.AUDIENCE_SYNC) == SECRET


@pytest.mark.parametrize("code,detail,expected", [
    (grpc.StatusCode.UNAUTHENTICATED, "the Git platform rejected the token", authz_client.TokenRejected),
    (grpc.StatusCode.UNAUTHENTICATED, "invalid grant: invalid signature", authz_client.Unavailable),
    (grpc.StatusCode.UNAUTHENTICATED, "a signed grant is required", authz_client.Unavailable),
    (grpc.StatusCode.NOT_FOUND, "no rule syncs to this datasource", authz_client.NoSuchRule),
    (grpc.StatusCode.INVALID_ARGUMENT, "say which Git platform", authz_client.BadQuestion),
    (grpc.StatusCode.UNAVAILABLE, "cannot ask the Git platform: boom", authz_client.Unavailable),
    (grpc.StatusCode.DEADLINE_EXCEEDED, "", authz_client.Unavailable),
    (grpc.StatusCode.INTERNAL, "policy evaluation error: x", authz_client.Unavailable),
])
def test_each_failure_means_what_the_caller_needs_to_know(engine, code, detail, expected):
    client, servicer = engine
    servicer.fail, servicer.detail = code, detail
    with pytest.raises(expected):
        client.decide([("gitea", "tok")], "read", rule=RULE)


def test_an_engine_nobody_answers_for_is_unavailable_with_no_detail_to_leak():
    client = authz_client.Client("127.0.0.1:1", timeout=1)
    with pytest.raises(authz_client.Unavailable) as caught:
        client.identify(("gitea", "tok"))
    assert "tok" not in str(caught.value)


def test_the_shared_client_needs_an_address(monkeypatch):
    from app.config import settings
    authz_client.set_client(None)
    monkeypatch.setattr(settings, "GATEWAY_AUTHZ_ADDR", "")
    with pytest.raises(authz_client.Unavailable, match="GATEWAY_AUTHZ_ADDR"):
        authz_client.client()
    monkeypatch.setattr(settings, "GATEWAY_AUTHZ_ADDR", "gateway:5658")
    try:
        assert authz_client.client().address == "gateway:5658"
    finally:
        authz_client.set_client(None)


def test_the_rules_own_branch_and_file_path_environments_reach_the_engine():
    from app.authz_client import rule_facts

    facts = rule_facts({"platform": "gitea", "datasource": "mongo", "branch": "release",
                        "file_path_map": {"team-b": "team-b/app.yaml", "team-a": "team-a/app.yaml"}})
    assert facts.branch == "release" and list(facts.file_path_envs) == ["team-a", "team-b"]


def test_a_rule_that_names_no_branch_is_read_on_main_as_self_heal_reads_it():
    from app.authz_client import rule_facts

    facts = rule_facts({"platform": "gitea", "datasource": "mongo"})
    assert facts.branch == "main" and list(facts.file_path_envs) == []
