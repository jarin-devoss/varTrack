"""
tests/unit/test_env_slice.py
─────────────────────────────
A file that holds the settings of every environment (app/pipeline/env_slice.py).

  - the two rules, at the function level: the root, and everything under it;
  - the merge, the rule's environments, and an environment the file does not name;
  - the 14 files of e2e/configs/env-keyed/ (7 cases, a simple and a complicated file each), run through the same ETL a
    sync and `vt` use, against the keys each environment must hold (their `expected/<env>.json`, written by hand).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.pipeline import stage_etl
from app.pipeline.env_slice import (
    Environments, NoEnvironment, deep_merge, detect_pattern, resolve_env_slice, rule_environments,
)
from app.pipeline.models import PayloadContext
from app.pipeline.stage_etl import run_stage_etl_content

CASES = Path(__file__).resolve().parents[3] / "e2e" / "configs" / "env-keyed"


def slice_(data, env, *environments):
    return resolve_env_slice(data, env, set(environments))


# ── the root ──────────────────────────────────────────────────────────────────

class TestRootOfEnvironmentObjects:
    DATA = {"predev": {"age": 44, "color": "red", "name": "dan"}, "default": {"age": 32, "color": "blue"}}

    def test_known_env(self):
        assert slice_(self.DATA, "predev") == {"age": 44, "color": "red", "name": "dan"}

    def test_default_for_an_env_the_file_does_not_name(self):
        assert slice_(self.DATA, "staging") == {"age": 32, "color": "blue"}

    def test_the_env_inherits_what_default_has(self):
        data = {"predev": {"name": "dan"}, "default": {"name": "bob", "age": 32}}
        assert slice_(data, "predev") == {"name": "dan", "age": 32}

    def test_detect(self):
        assert detect_pattern(self.DATA, "predev") == "pattern_1"


class TestTheBase:
    """Settings that belong to no environment are shared by all of them."""
    DATA = {"prod": {"name": "worn"}, "predev": {"name": "dan"}, "name": "bob", "age": 33}
    ENVS = ("prod", "predev", "staging")

    def test_each_env_overrides_the_base(self):
        assert slice_(self.DATA, "prod", *self.ENVS) == {"name": "worn", "age": 33}
        assert slice_(self.DATA, "predev", *self.ENVS) == {"name": "dan", "age": 33}

    def test_an_env_the_file_does_not_name_gets_the_base_only_never_another_envs_keys(self):
        assert slice_(self.DATA, "staging", *self.ENVS) == {"name": "bob", "age": 33}

    def test_default_is_the_fallback_over_the_base(self):
        data = {"prod": {"name": "worn"}, "default": {"name": "bob"}, "age": 33}
        assert slice_(data, "staging", "prod") == {"name": "bob", "age": 33}

    def test_detect(self):
        assert detect_pattern(self.DATA, "prod", set(self.ENVS)) == "pattern_3"

    def test_a_per_key_map_in_the_base_does_not_hide_the_environment_objects(self):
        """The case the old detection got wrong: the root also holds `log_level: {default, production}`."""
        data = {"log_level": {"default": "info", "production": "warn"}, "debug": False,
                "default": {"replicas": 2}, "production": {"replicas": 6}}
        assert slice_(data, "production", "production", "staging") == {"log_level": "warn", "debug": False, "replicas": 6}
        assert slice_(data, "staging", "production", "staging") == {"log_level": "info", "debug": False, "replicas": 2}


class TestAnEnvironmentTheFileDoesNotName:
    def test_nothing_for_it_and_no_default_raises(self):
        data = {"production": {"db": "p"}, "staging": {"db": "s"}}
        with pytest.raises(NoEnvironment) as raised:
            slice_(data, "qa", "production", "staging")
        assert raised.value.path == ""

    def test_with_a_default_it_is_not_an_error(self):
        data = {"production": {"db": "p"}, "default": {"db": "d"}}
        assert slice_(data, "qa", "production") == {"db": "d"}

    def test_a_nested_map_without_the_env_or_default_raises_and_says_where(self):
        data = {"database": {"host": {"production": "p", "staging": "s"}}}
        with pytest.raises(NoEnvironment) as raised:
            slice_(data, "qa", "production", "staging")
        assert raised.value.path == "database.host"

    def test_a_key_of_a_branch_deleted_since_is_never_written_as_base_settings(self):
        """branch_as_env: `old-feature` is no longer a branch, so it is not in the rule's names. A root of objects only is
        keyed by environment whatever its names, so its settings are not written under another environment."""
        data = {"default": {"a": 1}, "main": {"a": 2}, "old-feature": {"b": 9}}
        assert slice_(data, "main", "main") == {"a": 2}
        assert slice_(data, "develop", "main", "develop") == {"a": 1}


# ── under the root ────────────────────────────────────────────────────────────

class TestEnvironmentMaps:
    def test_a_value_per_environment(self):
        data = {"color": {"default": "blue", "predev": "green", "dev": "yellow"}, "age": 43}
        assert slice_(data, "predev", "predev", "dev") == {"color": "green", "age": 43}
        assert slice_(data, "staging", "predev", "dev") == {"color": "blue", "age": 43}
        assert detect_pattern(data, "predev", {"predev", "dev"}) == "pattern_2"

    def test_at_depth_three(self):
        data = {"a": {"b": {"c": {"default": 1, "production": 2}}}}
        assert slice_(data, "production") == {"a": {"b": {"c": 2}}}
        assert slice_(data, "staging", "production") == {"a": {"b": {"c": 1}}}
        assert slice_(data, "staging") == data        # `production` is not an environment of a rule that does not name it

    def test_inside_a_list_item(self):
        data = {"upstreams": [{"name": "a", "weight": {"default": 1, "production": 5}}, {"name": "b", "weight": 1}]}
        assert slice_(data, "production") == {"upstreams": [{"name": "a", "weight": 5}, {"name": "b", "weight": 1}]}

    def test_an_object_with_default_and_a_key_that_is_not_an_environment_is_written_whole(self):
        data = {"timeouts": {"default": 30, "upload": 300}, "retry": {"default": {"max": 3}, "payments": {"max": 5}}}
        assert slice_(data, "production") == data

    def test_the_same_object_is_resolved_when_that_key_is_an_environment(self):
        data = {"timeouts": {"default": 30, "upload": 300}}
        assert slice_(data, "staging", "upload") == {"timeouts": 30}      # `upload` is an environment of this rule

    def test_both_default_and_the_env_objects_are_merged(self):
        data = {"db": {"default": {"host": "a", "port": 1}, "production": {"host": "b"}}}
        assert slice_(data, "production") == {"db": {"host": "b", "port": 1}}

    def test_a_flat_dict_is_returned_as_it_is(self):
        data = {"host": "localhost", "port": 5432}
        assert slice_(data, "production") == data
        assert detect_pattern(data, "production") == "none"

    def test_empty_and_non_dict_input(self):
        assert resolve_env_slice({}, "production") == {}
        assert resolve_env_slice("not a dict", "production") == "not a dict"
        assert detect_pattern({"host": "localhost"}, "production") == "none"

    def test_a_root_default_that_is_a_setting_is_not_an_environment_object(self):
        assert slice_({"default": 5, "x": 1}, "production") == {"default": 5, "x": 1}


# ── the merge ─────────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_objects_merge_key_by_key_at_every_depth(self):
        assert deep_merge({"a": {"b": 1, "c": {"d": 2, "e": 3}}}, {"a": {"c": {"d": 9}}}) == {"a": {"b": 1, "c": {"d": 9, "e": 3}}}

    def test_a_list_is_replaced_never_concatenated(self):
        assert deep_merge({"origins": ["a", "b"]}, {"origins": ["c"]}) == {"origins": ["c"]}

    def test_null_replaces(self):
        assert deep_merge({"a": 1, "b": {"c": 2}}, {"a": None, "b": None}) == {"a": None, "b": None}

    def test_a_scalar_replaces_an_object_and_an_object_replaces_a_scalar(self):
        assert deep_merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}
        assert deep_merge({"a": 5}, {"a": {"b": 1}}) == {"a": {"b": 1}}

    def test_inputs_are_not_changed(self):
        base, over = {"a": {"b": 1}}, {"a": {"c": 2}}
        deep_merge(base, over)
        assert base == {"a": {"b": 1}} and over == {"a": {"c": 2}}


# ── the rule's environments ───────────────────────────────────────────────────

class TestRuleEnvironments:
    def test_branch_map_values_and_file_path_map_keys(self):
        rule = {"branch_map": {"main": "production", "develop": "staging"}, "file_path_map": {"qa": "configs/qa.yaml"}}
        envs = rule_environments(rule)
        assert "production" in envs and "staging" in envs and "qa" in envs
        assert "main" not in envs and "develop" not in envs                 # a branch name is not an environment here

    def test_the_environment_being_synced_and_default_are_always_names(self):
        assert resolve_env_slice({"a": {"default": 1, "weird": 2}}, "weird", rule_environments({}, "weird")) == {"a": 2}

    def test_branch_as_env_takes_the_repositorys_branches(self):
        envs = rule_environments({"branch_as_env": True}, "main", branches=["main", "develop", "feature/login"])
        assert all(name in envs for name in ("main", "develop", "feature/login")) and "other" not in envs

    def test_branches_count_only_with_branch_as_env(self):
        assert "develop" not in rule_environments({"branch_map": {"main": "production"}}, "production", branches=["develop"])

    def test_tag_as_env_takes_the_repositorys_tags(self):
        envs = rule_environments({"tag_as_env": True}, "v1", tags=["v1", "v2"])
        assert "v2" in envs and "main" not in envs

    def test_pr_as_env_is_the_shape_pr_number(self):
        envs = rule_environments({"pr_as_env": True}, "pr-42")
        assert "pr-42" in envs and "pr-7" in envs and "pr-" not in envs and "pr-x" not in envs and "pr-4a" not in envs
        assert "pr-7" not in rule_environments({}, "pr-42")

    def test_a_layered_rule_declares_what_each_layer_adds(self):
        """config.cue's branch_map, merged key by key with a vartrack.json that maps one more branch."""
        layered = {"branch_map": {"main": "production", "release": "preview"}}
        assert "preview" in rule_environments(layered, "production")

    def test_an_environment_named_default_is_the_files_own_default(self):
        """branch_map: {main: "default"}: the rule's environment is called default; the file's `default` is that environment."""
        data = {"default": {"a": 1}, "staging": {"a": 2}}
        assert slice_(data, "default", "default", "staging") == {"a": 1}
        assert slice_(data, "staging", "default", "staging") == {"a": 2}

    def test_membership_of_a_non_string_is_false(self):
        assert 5 not in Environments({"a"}) and None not in Environments({"a"})


# ── the repository's refs, from the mirror ───────────────────────────────────

@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_mirror_lists_its_branches_and_tags_without_the_remote(tmp_path):
    from app.utils.git.mirror import RepoMirrors

    src = tmp_path / "src"
    src.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (src / "a").write_text("x")
    git("add", ".")
    git("commit", "-qm", "1")
    git("branch", "develop")
    git("branch", "feature/login")
    git("tag", "v1.0.0")
    mirrors = RepoMirrors(root=tmp_path / "mirrors")
    assert mirrors.refs(str(src), "heads") == []                            # no mirror yet
    mirrors.ensure(str(src), None)
    assert sorted(mirrors.refs(str(src), "heads")) == ["develop", "feature/login", "main"]
    assert mirrors.refs(str(src), "tags") == ["v1.0.0"]
    with pytest.raises(ValueError):
        mirrors.refs(str(src), "remotes")


# ── the 14 files of e2e/configs/env-keyed/ ───────────────────────────────────

class _NoSchemas:
    def current_schema_root(self, tenant_id):
        return Path("/nonexistent")

    def schema_commit(self, tenant_id):
        return ""


class _Mirrors:
    def __init__(self, branches):
        self.branches = branches

    def refs(self, url, kind):
        return list(self.branches) if kind == "heads" else []


def case_dirs():
    return sorted(p.parent for p in CASES.glob("*/*/case.json"))


def load_case(directory: Path):
    case = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    expected = {}
    for env in case["branches"].values():
        path = directory / "expected" / f"{env.replace('/', '_')}.json"
        if path.exists():
            expected[env] = json.loads(path.read_text(encoding="utf-8"))
    return case, expected


def run_case(monkeypatch, directory: Path, env: str, strict: bool = False):
    import app.schema_registry.manager as manager
    import app.utils.git.mirror as mirror

    case, _ = load_case(directory)
    monkeypatch.setattr(manager, "get_schema_manager", lambda *a, **k: _NoSchemas())
    monkeypatch.setattr(mirror, "get_default_mirrors", lambda: _Mirrors(case["branches"]))
    rule = {"platform": "gitea", "datasource": "mongo", **case["rule"]}
    if not rule.get("branch_as_env"):
        rule["branch_map"] = case["branches"]
    if strict:
        rule["strict_validation"] = True
    ctx = PayloadContext(platform="gitea", repo_url="https://git/acme/configs", ref="refs/heads/main", branch="main",
                         commit_sha="abc", tag=None, pr_number=None, rule_config=rule, parsed_payload={})
    name = case["file"]
    return run_stage_etl_content(ctx=ctx, file_path=f"env-keyed/{name}", content=(directory / name).read_text(encoding="utf-8"),
                                 fmt=name.rsplit(".", 1)[1], env=env, tenant_id="default")


def test_there_are_seven_cases_with_a_simple_and_a_complicated_file_each():
    assert [(d.parent.name, d.name) for d in case_dirs()] == [
        (case, variant) for case in sorted({d.parent.name for d in case_dirs()}) for variant in ("complicated", "simple")]
    assert len(case_dirs()) == 14 and len({d.parent.name for d in case_dirs()}) == 7


def _params():
    out = []
    for directory in case_dirs():
        case, expected = load_case(directory)
        for env in case["branches"].values():
            out.append(pytest.param(directory, env, id=f"{directory.parent.name}/{directory.name}/{env}"))
    return out


@pytest.mark.parametrize("directory, env", _params())
def test_each_file_gives_each_environment_the_keys_it_must_hold(monkeypatch, directory, env):
    case, expected = load_case(directory)
    etl = run_case(monkeypatch, directory, env)
    if env in case.get("nothing_for", []):
        assert env not in expected
        assert etl.files == [] and etl.errors == []
        (reason,) = etl.unfed.values()
        assert f"names no environment {env!r} and no 'default'" in reason
        return
    assert etl.errors == [], etl.error_reasons
    assert len(etl.files) == 1, (etl.skipped, etl.unfed)
    assert dict(etl.files[0].flat_data) == expected[env]


def test_an_environment_the_file_does_not_name_fails_the_file_with_strict_validation(monkeypatch):
    directory = CASES / "4-no-default" / "simple"
    etl = run_case(monkeypatch, directory, "qa", strict=True)
    assert etl.files == [] and etl.unfed == {}
    (reason,) = etl.error_reasons.values()
    assert reason.startswith("strict_validation: ") and "names no environment 'qa' and no 'default'" in reason


def test_another_environments_keys_or_secrets_are_never_written_under_an_environment(monkeypatch):
    for directory in case_dirs():
        case, expected = load_case(directory)
        for env in case.get("nothing_for", []):
            etl = run_case(monkeypatch, directory, env)
            assert etl.files == []
        for env, flat in expected.items():
            for other in case["branches"].values():
                if other != env and other in ("production", "staging"):
                    assert not any(key.startswith(f"{other}.") for key in flat), (directory, env, other)
