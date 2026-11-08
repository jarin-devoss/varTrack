"""Which schema checks which file: the lookup order of plan 08 §2.3, the glob of schema_map, and the paths it refuses."""
from __future__ import annotations

import os

import pytest

from app.pipeline import schema_paths
from app.pipeline.validator import Validator


@pytest.fixture
def repo(tmp_path):
    """A tenant's schema repository."""
    for name in ("app.yaml.cue", "default.cue", "api/settings.cue", "web.cue", "other.cue"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    return tmp_path


MAP = [
    {"match": "services/api/**/*.yaml", "schema": "api/settings.cue"},
    {"match": "services/web/config.yaml", "schema": "web.cue"},
    {"match": "legacy/**", "schema": "none"},
    {"match": "missing/*.yaml", "schema": "nowhere.cue"},
]


def choose(repo, file_path, *, file_schema=None, schema_from_file=False, schema_map=MAP, schema_dir=True):
    rule = {"schema_map": schema_map, "schema_from_file": schema_from_file}
    validator = Validator(schema_dir=repo if schema_dir else None)
    return validator.choose(file_path, rule, file_schema, tenant_id="acme", commit="abcdef0123456789")


# ── the lookup order, row by row ─────────────────────────────────────────────

@pytest.mark.parametrize("file_path,name,source", [
    ("services/api/v1/app.yaml",  "api/settings.cue", "schema_map[0]"),   # step 1, ** crosses folders
    ("services/api/app.yaml",     "api/settings.cue", "schema_map[0]"),   # ** also matches no folder
    ("services/web/config.yaml",  "web.cue",          "schema_map[1]"),   # step 1, an exact path
    ("services/web/app.yaml",     "app.yaml.cue",     "name"),            # not in the map: the file's name, as before
    ("configs/prod/app.yaml",     "app.yaml.cue",     "name"),
    ("configs/prod/other.yaml",   "default.cue",      "default"),         # step 4
])
def test_the_schema_a_file_gets(repo, file_path, name, source):
    choice = choose(repo, file_path)
    assert (choice.name, choice.source, choice.error) == (name, source, "")
    assert choice.path == (repo / name).resolve() or choice.path == repo / name
    assert choice.checked


def test_no_schema_at_all_is_none_not_ok(repo):
    (repo / "default.cue").unlink()
    choice = choose(repo, "configs/prod/other.yaml")
    assert (choice.path, choice.name, choice.source, choice.error) == (None, "none", "none", "")
    assert not choice.checked


def test_a_map_entry_of_none_is_an_explicit_not_checked(repo):
    choice = choose(repo, "legacy/old/app.yaml")                     # app.yaml.cue exists, and is not used
    assert (choice.path, choice.name, choice.source, choice.error) == (None, "none", "schema_map[2]", "")


def test_the_first_matching_entry_wins(repo):
    both = [{"match": "**/*.yaml", "schema": "web.cue"}, {"match": "services/api/**", "schema": "api/settings.cue"}]
    assert choose(repo, "services/api/a.yaml", schema_map=both).source == "schema_map[0]"


def test_a_mapped_schema_that_is_missing_fails_the_file_and_names_everything(repo):
    choice = choose(repo, "missing/app.yaml")
    assert choice.path is None
    assert choice.error == ("schema nowhere.cue (schema_map[3], missing/*.yaml) is not in the schema repository "
                            "of tenant acme at abcdef012345")
    assert (choice.name, choice.source) == ("nowhere.cue", "schema_map[3]")


def test_a_mapped_schema_with_no_schema_repository_fails_too(repo):
    choice = choose(repo, "services/web/config.yaml", schema_dir=False)
    assert "is not in the schema repository of tenant acme" in choice.error


def test_none_needs_no_schema_repository(repo):
    assert choose(repo, "legacy/a.yaml", schema_dir=False).source == "schema_map[2]"


# ── the file's own $schema ───────────────────────────────────────────────────

def test_a_file_names_its_schema_only_when_the_rule_allows_it(repo):
    off = choose(repo, "x/app.yaml", file_schema="web.cue", schema_from_file=False)
    assert (off.name, off.source) == ("app.yaml.cue", "name")      # ignored: the writer does not choose
    on = choose(repo, "x/app.yaml", file_schema="web.cue", schema_from_file=True)
    assert (on.name, on.source, on.note) == ("web.cue", "file", "")


def test_the_map_outranks_the_file_and_the_result_says_so(repo):
    choice = choose(repo, "services/web/config.yaml", file_schema="other.cue", schema_from_file=True)
    assert (choice.name, choice.source) == ("web.cue", "schema_map[1]")
    assert choice.note == "the file's $schema (other.cue) was overridden by schema_map[1]"


def test_the_same_schema_named_twice_needs_no_note(repo):
    choice = choose(repo, "services/web/config.yaml", file_schema="web.cue", schema_from_file=True)
    assert choice.note == ""


def test_a_file_that_names_a_missing_schema_fails(repo):
    choice = choose(repo, "x/app.yaml", file_schema="gone.cue", schema_from_file=True)
    assert "schema gone.cue ($schema of the file) is not in the schema repository of tenant acme" in choice.error


def test_a_file_that_names_none_is_not_checked(repo):
    choice = choose(repo, "x/app.yaml", file_schema="none", schema_from_file=True)
    assert (choice.path, choice.name, choice.source) == (None, "none", "file")


def test_a_url_is_an_editors_hint_not_a_schema(repo):
    url = "https://json.schemastore.org/github-workflow.json"
    choice = choose(repo, "x/app.yaml", file_schema=url, schema_from_file=True)
    assert (choice.name, choice.source) == ("app.yaml.cue", "name")  # went on to the name lookup
    assert "is a URL" in choice.note and url in choice.note


def test_a_non_string_schema_key_is_ignored(repo):
    assert choose(repo, "x/app.yaml", file_schema={"a": 1}, schema_from_file=True).source == "name"


# ── a path from a configuration never leaves the schema repository ───────────

@pytest.mark.parametrize("schema", ["../outside.cue", "api/../../outside.cue", "/etc/passwd.cue", "C:/x.cue",
                                    "api\\settings.cue", "api//settings.cue", "settings"])
def test_a_schema_path_that_could_escape_is_refused(repo, schema):
    choice = choose(repo, "a.yaml", schema_map=[{"match": "*.yaml", "schema": schema}])
    assert choice.path is None and choice.error and "schema" in choice.error


def test_a_symlink_that_points_out_of_the_clone_is_refused(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.cue"
    outside.write_text("{}\n")
    try:
        os.symlink(outside, repo / "link.cue")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    choice = choose(repo, "a.yaml", schema_map=[{"match": "*.yaml", "schema": "link.cue"}])
    assert choice.path is None and "leaves the schema repository" in choice.error


def test_join_inside_keeps_a_good_path(repo):
    assert schema_paths.join_inside(repo, "api/settings.cue") == (repo / "api" / "settings.cue").resolve()


# ── the glob ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern,path,expected", [
    ("*.yaml", "app.yaml", True),
    ("*.yaml", "dir/app.yaml", False),                  # * stays inside one folder
    ("**/*.yaml", "dir/app.yaml", True),
    ("**/*.yaml", "app.yaml", True),                    # **/ is zero or more folders
    ("**/*.yaml", "a/b/c/app.yaml", True),
    ("a/**", "a/b/c.yaml", True),
    ("a/**", "a", False),
    ("a/**/z.yaml", "a/z.yaml", True),
    ("a/**/z.yaml", "a/b/c/z.yaml", True),
    ("a/**/z.yaml", "ab/z.yaml", False),
    ("a/*/z.yaml", "a/b/c/z.yaml", False),
    (".env", ".env", True),                             # dotfiles match
    ("*", ".env", True),
    ("**/.env", "deploy/.env", True),
    ("app.yaml", "App.yaml", False),                    # case counts
    ("app-?.yaml", "app-1.yaml", True),
    ("app-?.yaml", "app-12.yaml", False),
    ("app-?.yaml", "app-/.yaml", False),                # ? is not a slash
    ("app-[ab].yaml", "app-a.yaml", True),
    ("app-[ab].yaml", "app-c.yaml", False),
    ("app-[!ab].yaml", "app-c.yaml", True),
    ("a.b+c(d).yaml", "a.b+c(d).yaml", True),           # regex characters are literal
    ("a.b", "axb", False),
    ("services/api/app.yaml", "/services/api/app.yaml", True),    # a leading slash on the path is dropped
    ("services\\api\\app.yaml", "services/api/app.yaml", False),
])
def test_glob_match(pattern, path, expected):
    assert schema_paths.glob_match(pattern, path) is expected


def test_a_windows_path_is_matched_with_forward_slashes():
    assert schema_paths.glob_match("services/api/*.yaml", "services\\api\\app.yaml")


# ── the load-time checks ─────────────────────────────────────────────────────

def test_every_bad_entry_is_named():
    problems = schema_paths.schema_map_problems([
        {"match": "ok/**", "schema": "a.cue"},
        {"match": "", "schema": "../x.cue"},
        {"match": "a[", "schema": "none", "extra": 1},
        "not an entry",
        {"match": 5, "schema": ["a.cue"]},
    ], "rules[0].schema_map")
    joined = "\n".join(problems)
    for want in ("schema_map[1].match", "is empty", 'schema_map[1].schema "../x.cue"', '".." segment',
                 'schema_map[2].match "a["', "no closing", "schema_map[2] has extra", "schema_map[3] must be",
                 "schema_map[4].match must be a string", "schema_map[4].schema must be a string"):
        assert want in joined, want
    assert not any("schema_map[0]" in p for p in problems)


def test_a_missing_or_good_schema_map_has_no_problems():
    assert schema_paths.schema_map_problems(None) == []
    assert schema_paths.schema_map_problems([{"match": "a/**", "schema": "none"}]) == []
    assert schema_paths.schema_map_problems({"match": "a"}) == ["schema_map must be a list of {match, schema}, not dict"]
