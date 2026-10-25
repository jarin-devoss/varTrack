"""
Presets (app.pipeline.presets): bundle.json, its rules and a repository's
vartrack.json name shared settings files in "extends".
"""
from __future__ import annotations

import json
import logging

import pytest

from app.pipeline import datasource_settings as ds
from app.pipeline.presets import Origin, PresetError, Resolver, parse_preset, resolve_bundle_rules

FILES = {
    ("http://gitea:3000/acme/presets.git", "main", "default.json"):
        {"description": "org baseline", "prune": {"protection": ["_vt_*"]}},
    ("http://gitea:3000/acme/presets.git", "main", "production.json"):
        {"extends": ["local>acme/presets"], "prune": {"protection": ["SYSTEM_*"]}, "sync_mode": "AUTO"},
    ("http://gitea:3000/acme/presets.git", "v2", "teams/payments.json"):
        {"extends": ["acme/presets:production"], "root_key": "payments"},
    ("http://gitea:3000/acme/presets.git", "main", "loop.json"):
        {"extends": ["acme/presets:loop"], "dry_run": True},
    ("https://git.example.com/acme/shared.git", "main", "default.json"):
        {"branch_map": {"develop": "staging"}},
}


def fetch(url, ref, path, token):
    data = FILES.get((url, ref, path))
    return None if data is None else json.dumps(data)


ORIGIN = Origin.of("http://oauth2:tok@gitea:3000/acme/schemas.git", "tok")


@pytest.mark.parametrize("text,want", [
    ("local>acme/presets", ("local", "acme/presets", "default.json", "main")),
    ("acme/presets:production", ("", "acme/presets", "production.json", "main")),
    ("gitea-team>acme/presets//teams/payments#v2", ("gitea-team", "acme/presets", "teams/payments.json", "v2")),
    ("acme/presets:base.json", ("", "acme/presets", "base.json", "main")),
])
def test_parse_preset(text, want):
    p = parse_preset(text)
    assert (p.source, p.repo, p.file, p.ref) == want


@pytest.mark.parametrize("text", ["presets", ":base", "acme/presets:a//b", "acme/presets:x y"])
def test_parse_preset_rejects(text):
    with pytest.raises(PresetError):
        parse_preset(text)


def test_origin_drops_credentials():
    assert ORIGIN.base_url == "http://gitea:3000"


def test_presets_merge_left_to_right_under_the_config():
    out = Resolver(ORIGIN, fetch).resolve({
        "extends": ["local>acme/presets//teams/payments#v2"],
        "root_key": "app",
        "prune": {"enabled": True, "protection": ["ledger.*"]},
    })
    assert out == {
        "prune": {"enabled": True, "protection": ["_vt_*", "SYSTEM_*", "ledger.*"]},
        "sync_mode": "AUTO",
        "root_key": "app",
    }


def test_a_preset_already_in_the_chain_is_skipped():
    assert Resolver(ORIGIN, fetch).resolve({"extends": ["acme/presets:loop"]}) == {"dry_run": True}


def test_a_platform_prefix_reads_from_that_platform(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: {"platforms": [
        {"github": {"tag": "ent", "endpoint": "https://git.example.com/", "token": {"value": "ghp"}}}]})
    seen = []

    def spy(url, ref, path, token):
        seen.append(token)
        return fetch(url, ref, path, token)

    out = Resolver(ORIGIN, spy).resolve({"extends": ["github-ent>acme/shared"]})
    assert out == {"branch_map": {"develop": "staging"}}
    assert seen == ["ghp"]


def test_missing_preset_and_unknown_platform(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: {})
    with pytest.raises(PresetError, match="no nope.json"):
        Resolver(ORIGIN, fetch).resolve({"extends": ["acme/presets:nope"]})
    with pytest.raises(PresetError, match="no platform 'gitlab'"):
        Resolver(ORIGIN, fetch).resolve({"extends": ["gitlab>acme/presets"]})


def test_bundle_json_extends_for_every_rule_and_per_rule():
    rules = resolve_bundle_rules({
        "extends": ["local>acme/presets"],
        "rules": [
            {"platform": "gitea", "datasource": "mongo"},
            {"platform": "gitea", "datasource": "redis", "extends": ["acme/presets:production"], "sync_mode": "LIVE_STATE"},
        ],
    }, ORIGIN, "bundle.json", fetch)
    assert rules == [
        {"platform": "gitea", "datasource": "mongo", "prune": {"protection": ["_vt_*"]}},
        {"platform": "gitea", "datasource": "redis", "prune": {"protection": ["_vt_*", "SYSTEM_*"]},
         "sync_mode": "LIVE_STATE"},
    ]


def test_one_unreadable_preset_makes_the_bundle_unusable():
    with pytest.raises(PresetError, match="gitea/zk rule: preset acme/presets:nope"):
        resolve_bundle_rules({"rules": [
            {"platform": "gitea", "datasource": "mongo"},
            {"platform": "gitea", "datasource": "zk", "extends": "acme/presets:nope"},
        ]}, ORIGIN, "bundle.json", fetch)


def test_a_plain_list_needs_no_origin():
    rules = [{"platform": "gitea", "datasource": "mongo"}]
    assert resolve_bundle_rules(rules, None, "bundle.json", fetch) == rules


def test_vartrack_json_extends(monkeypatch):
    from unittest.mock import MagicMock, patch

    from app.pipeline import presets
    from app.pipeline.models import PayloadContext
    from app.pipeline.repo_overrides import apply_repo_overrides

    monkeypatch.setattr(presets, "_git_fetch", fetch)
    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/acme/app.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"prune": {"enabled": True}}, parsed_payload={})
    vartrack_json = json.dumps({"extends": ["local>acme/presets:production"], "platform": "x"})
    with patch("app.pipeline.git_extractor.GitExtractor") as extractor:
        extractor.return_value = MagicMock(extract_file=MagicMock(return_value=vartrack_json))
        out = apply_repo_overrides(ctx).rule_config
    assert out == {"prune": {"enabled": True, "protection": ["_vt_*", "SYSTEM_*"]}, "sync_mode": "AUTO"}


def test_local_inside_a_preset_is_that_preset_s_server(monkeypatch):
    monkeypatch.setattr(ds, "_bundle", lambda: {"platforms": [{"gitea": {"endpoint": "http://gitea:3000"}}]})
    team_origin = Origin.of("http://gitea-team:3000/payments/schemas.git")
    out = Resolver(team_origin, fetch).resolve({"extends": ["gitea>acme/presets:production"]})
    assert out == {"prune": {"protection": ["_vt_*", "SYSTEM_*"]}, "sync_mode": "AUTO"}


def test_vartrack_json_under_another_name():
    from unittest.mock import MagicMock, patch

    from app.pipeline.models import PayloadContext
    from app.pipeline.repo_overrides import apply_repo_overrides

    files = {".gitea/vartrack.json": json.dumps({"root_key": "gitea"}),
             ".gitlab/vartrack.json": json.dumps({"root_key": "gitlab"})}
    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/acme/app.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={}, parsed_payload={})
    with patch("app.pipeline.git_extractor.GitExtractor") as extractor:
        extractor.return_value = MagicMock(extract_file=MagicMock(side_effect=lambda u, r, name, token: files.get(name)))
        assert apply_repo_overrides(ctx).rule_config == {"root_key": "gitea"}


@pytest.mark.parametrize("content,match", [
    ('{"dry_run": true,', "not valid JSON"),
    ('["dry_run"]', "must be a JSON object"),
    ('{"extends": ["local>acme/presets:nope"]}', "no nope.json"),
])
def test_an_unusable_vartrack_json_stops_the_sync(monkeypatch, content, match):
    # An invalid vartrack.json: no sync without the settings asked for.
    from unittest.mock import MagicMock, patch

    from app.pipeline import presets
    from app.pipeline.models import PayloadContext
    from app.pipeline.repo_overrides import RepoConfigError, apply_repo_overrides

    monkeypatch.setattr(presets, "_git_fetch", fetch)
    ctx = PayloadContext(platform="gitea", repo_url="http://gitea:3000/acme/app.git", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={}, parsed_payload={})
    with patch("app.pipeline.git_extractor.GitExtractor") as extractor:
        extractor.return_value = MagicMock(extract_file=MagicMock(return_value=content))
        with pytest.raises(RepoConfigError, match=match):
            apply_repo_overrides(ctx)
