"""
tests/unit/test_formats.py

A FILE sink writes the file in its own format, or the one the rule names (app/parsers/formats.py, sinks/file_output.py).
The fixtures are the e2e's (e2e/configs/formats): the same settings in every format the parsers read.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

for _m in ("pymongo", "git", "gitpython"):
    sys.modules.setdefault(_m, MagicMock())

import pytest

from app.parsers import detector, formats, loader
from app.parsers.formats import Unrenderable
from app.pipeline.sinks.file_output import file_output
from app.utils.enums.sync_mode import SyncMode

FIXTURES = Path(__file__).resolve().parents[3] / "e2e" / "configs" / "formats"
BASE = {  # the format a rule calls it -> the fixture that has it
    "yaml": "app.yaml", "json": "app.json", "toml": "app.toml", "hcl": "app.hcl", "xml": "app.xml",
    "ini": "app.ini", "env": "app.env", "properties": "app.properties",
}


def fixture(name: str) -> tuple[str, object, str]:
    """(the file as it is, its tree, the parser family that reads it)"""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text, loader.load(text, name), loader.format_of(text, name)


def keys_of(name: str) -> dict[str, str]:
    """The settings a fixture holds, flat, the way the ETL sees them but for HCL's quotes."""
    text, tree, family = fixture(name)
    return formats.flat(formats.clean_hcl(tree) if family == "hcl" else tree)


# ── The fixtures say the same thing ───────────────────────────────────────────

def test_every_format_has_a_fixture_with_the_same_ten_settings():
    expected = keys_of("app.yaml")
    assert len(expected) == 10 and expected["cache.backends.1"] == "memcached" and expected["database.tls"] == "true"
    for name in BASE.values():
        got = keys_of(name)
        if name == "app.xml":     # an XML file has a root element
            got = {k.removeprefix("config."): v for k, v in got.items()}
        assert got == expected, name


# ── Every format writes itself, and reads back as it was ──────────────────────

@pytest.mark.parametrize("target,name", BASE.items())
def test_a_tree_is_written_in_the_format_of_the_file_it_came_from(target, name):
    text, tree, family = fixture(name)
    out = formats.render(target, tree, source_family=family, source_text=text)
    assert formats.stored_flat(out, formats.TARGETS[target]) == formats.flat(tree)


# ── Any format into any other: the 8 x 8 table ────────────────────────────────

@pytest.mark.parametrize("target", BASE)
@pytest.mark.parametrize("source,name", BASE.items())
def test_a_file_is_converted_into_every_other_format(source, name, target):
    text, tree, family = fixture(name)
    out = formats.render(target, tree, source_family=family, source_text=text)
    # what the converted file says, read as its own format, is what the source said
    got = formats.stored_flat(out, formats.TARGETS[target], source_family=family)
    # (HCL into HCL reads with its quotes, as Git's own file does)
    assert got == formats.flat(tree if family == "hcl" and target == "hcl" else formats.clean_hcl(tree) if family == "hcl" else tree)
    # and it is a file in that format: its own parser reads it, and its name would say so
    assert formats.parse(out, formats.TARGETS[target]) is not None
    assert formats.format_named_by("x" + formats.TARGETS[target].suffix) == formats.TARGETS[target]


# ── What cannot be written is refused, naming the key ─────────────────────────

def test_null_cannot_be_written_as_toml():
    with pytest.raises(Unrenderable, match=r"database\.password is null, and TOML has no null"):
        formats.render("toml", {"database": {"password": None}}, source_family="yaml")


def test_a_key_that_is_not_an_xml_name_is_refused():
    with pytest.raises(Unrenderable, match=r"'1st' is not a name XML can use"):
        formats.render("xml", {"1st": "a"}, source_family="yaml")


def test_a_namespaced_element_cannot_come_back_out_of_a_converted_file():
    with pytest.raises(Unrenderable, match="not a name XML can use"):
        formats.render("xml", {"ns0:project": {"a": "1"}}, source_family="xml")


def test_ini_has_no_key_outside_a_section_next_to_sections():
    with pytest.raises(Unrenderable, match=r"'name' is outside any section"):
        formats.render("ini", {"name": "app", "database": {"host": "x"}}, source_family="yaml")


def test_a_value_on_two_lines_cannot_be_written_to_a_env_file():
    with pytest.raises(Unrenderable, match=r"'motd' would not read back from env"):
        formats.render("env", {"motd": "line one\nline two"}, source_family="yaml")


def test_toml_is_a_table():
    with pytest.raises(Unrenderable, match="a TOML file is a table"):
        formats.render("toml", ["a", "b"], source_family="yaml")


def test_a_format_that_is_not_one_is_a_value_error():
    with pytest.raises(ValueError, match="not a format varTrack writes"):
        formats.target("docx")


def test_tfvars_is_not_a_name_for_hcl():
    # A .tfvars file is read as key=value lines, so the quotes of its strings are not in its values; written as HCL
    # from those values, the strings would come back different. A rule cannot ask for it.
    with pytest.raises(ValueError, match="not a format varTrack writes"):
        formats.target("tfvars")
    assert formats.format_named_by("prod.tfvars") is None


@pytest.mark.parametrize("name,fmt", [("yml", "yaml"), (".json", "json"), ("TOML", "toml"), ("dotenv", "env"), ("cfg", "ini"), ("tf", "hcl")])
def test_a_rule_may_call_a_format_by_another_name(name, fmt):
    assert formats.target(name).name == fmt


# ── An XML file keeps its attributes ──────────────────────────────────────────

def test_a_web_config_keeps_the_values_that_are_attributes():
    text, tree, family = fixture("web.config")
    assert family == "xml"
    keys = formats.flat(tree)
    assert keys["configuration.appSettings.add.0.@attrs.key"] == "DbHost"
    assert keys["configuration.appSettings.add.0.@attrs.value"] == "mongo.prod.internal"
    assert keys["configuration.appSettings.add.1.@attrs.value"] == "27017"


def test_a_leaf_with_attributes_and_text_keeps_both_and_one_without_stays_its_text():
    tree = loader.load('<c><feature name="dark">true</feature><plain>x</plain><empty/></c>', "a.xml")
    assert tree == {"c": {"feature": {"@attrs": {"name": "dark"}, "#text": "true"}, "plain": "x", "empty": ""}}


def test_attributes_come_back_out_as_attributes():
    text, tree, family = fixture("web.config")
    out = formats.render("xml", tree, source_family=family, source_text=text)
    assert 'key="DbHost"' in out and 'value="mongo.prod.internal"' in out
    assert formats.flat(loader.load(out, "web.config")) == formats.flat(tree)


def test_a_dot_config_is_xml_or_ini_by_what_it_holds():
    assert loader.format_of("<settings><a>1</a></settings>", "machine.config") == "xml"
    assert loader.format_of("[db]\nhost = x\n", "tool.config") == "ini"
    assert loader.load("[db]\nhost = x\n", "tool.config") == {"db": {"host": "x"}}
    assert detector.detect("web.config") == "xml"      # by its name, as before


# ── The file as it is in Git, or a render of what the sync changed ────────────

@pytest.mark.parametrize("target,name", BASE.items())
def test_a_file_nothing_changed_is_written_as_it_is_in_git(target, name):
    text, tree, family = fixture(name)
    out = file_output(source_text=text, file_path=name, data=tree, flat_data=formats.flat(tree))
    assert out.copied and out.content == text.encode("utf-8") and out.name == name
    assert out.content_type == formats.TARGETS[target].content_type


def test_the_comments_of_a_copied_file_are_kept():
    text, tree, _ = fixture("app.yaml")
    out = file_output(source_text=text, file_path="app.yaml", data=tree, flat_data=formats.flat(tree))
    assert out.content.decode().startswith("# varTrack format fixture")


def test_a_file_a_sync_changed_is_rendered_in_its_own_format():
    text, tree, _ = fixture("app.yaml")
    changed = {**tree, "database": {**tree["database"], "password": "s3cret"}}
    out = file_output(source_text=text, file_path="app.yaml", data=changed, flat_data=formats.flat(changed))
    assert not out.copied and out.name == "app.yaml" and out.fmt.name == "yaml"
    assert loader.load(out.content.decode(), "app.yaml") == changed


def test_a_secret_in_an_hcl_file_is_quoted_when_the_file_is_rendered():
    text, tree, _ = fixture("app.hcl")
    changed = {**tree, "database": {**tree["database"], "host": "resolved-by-vault"}}   # a plain string: a secret's value
    out = file_output(source_text=text, file_path="app.hcl", data=changed, flat_data=formats.flat(changed))
    assert not out.copied and 'host = "resolved-by-vault"' in out.content.decode()


def test_a_rule_that_names_another_format_gets_a_converted_file_with_its_suffix():
    text, tree, _ = fixture("app.yaml")
    out = file_output(source_text=text, file_path="configs/app.yaml", data=tree, flat_data=formats.flat(tree), file_format="json")
    assert not out.copied and out.name == "app.json" and out.content_type == "application/json"
    assert loader.load(out.content.decode(), "app.json") == tree


def test_a_name_the_sink_was_given_says_the_format():
    text, tree, _ = fixture("app.yaml")
    out = file_output(source_text=text, file_path="app.yaml", data=tree, flat_data=formats.flat(tree), given_name="config.json")
    assert out.name == "config.json" and out.fmt.name == "json" and not out.copied


def test_a_name_with_no_extension_keeps_the_files_own_format():
    text, tree, _ = fixture("app.yaml")
    out = file_output(source_text=text, file_path="app.yaml", data=tree, flat_data=formats.flat(tree), given_name="config")
    assert out.copied and out.name == "config" and out.fmt.name == "yaml"


def test_the_flat_keys_alone_are_nested_by_their_dots_for_a_format_that_holds_a_tree():
    out = file_output(source_text="", file_path="app.yaml", data=None, flat_data={"db.host": "x", "db.port": "5"})
    assert loader.load(out.content.decode(), "app.yaml") == {"db": {"host": "x", "port": "5"}}


def test_a_file_whose_conversion_would_lose_a_key_fails_instead_of_being_written():
    text = "db:\n  password:\n"
    tree = loader.load(text, "app.yaml")
    with pytest.raises(Unrenderable, match=r"db\.password is null"):
        file_output(source_text=text, file_path="app.yaml", data=tree, flat_data=formats.flat(tree), file_format="toml")


def test_the_root_element_xml_needs_is_added_and_taken_away_again():
    text, tree, family = fixture("app.yaml")
    out = formats.render("xml", tree, source_family=family, source_text=text, xml_root="settings")
    assert out.startswith("<settings>")
    assert formats.stored_flat(out, formats.TARGETS["xml"], source_family=family, xml_root="settings") == formats.flat(tree)


def test_an_xml_declaration_and_the_indentation_of_the_source_are_kept():
    text, tree, family = fixture("app.xml")
    changed = {"config": {**tree["config"], "extra": "1"}}
    out = formats.render("xml", changed, source_family=family, source_text=text)
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <app>')


# ── The sinks ─────────────────────────────────────────────────────────────────

UPSERT = SyncMode.GIT_UPSERT_ALL
SMART = SyncMode.GIT_SMART_REPAIR
WRITE = dict(datasource="ds", env="prod", repo="r", branch="main", commit_sha="abc", total_sources=1)
LABELS = MagicMock(tenant_id="acme", as_s3_tags=lambda: "a=b")


def _s3(**cfg):
    from app.pipeline.sinks.s3 import S3Sink
    from app.pipeline.sinks.settings import Settings
    sink = S3Sink(settings=Settings.of("s3", bucket="b", prefix="cfg", update_strategy="STRATEGY_FILE", **cfg.pop("settings", {})),
                  rule_config=cfg)
    sink._s3.reset_mock(return_value=True, side_effect=True)    # the stubbed boto3 client is one object for every sink
    return sink


def _s3_write(sink, name, mode=UPSERT, changed=None):
    text, tree, _ = fixture(name)
    data = changed or tree
    return sink._write(flat_data=formats.flat(data), data=data, source_text=text, file_path=name, sync_mode=mode, labels=LABELS, **WRITE)


@pytest.mark.parametrize("name", BASE.values())
def test_s3_holds_the_file_as_it_is_in_git_under_its_own_name(name):
    sink = _s3()
    _s3_write(sink, name)
    put = sink._s3.put_object.call_args.kwargs
    assert put["Key"] == f"cfg/prod/{name}"
    assert put["Body"] == (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n") or put["Body"] == (FIXTURES / name).read_text(encoding="utf-8").encode()
    assert put["ContentType"] == formats.default_target(loader.format_of((FIXTURES / name).read_text(encoding="utf-8"), name), name).content_type


def test_s3_converts_the_file_when_the_rule_names_a_format():
    sink = _s3(file_format="json")
    _s3_write(sink, "app.xml")
    put = sink._s3.put_object.call_args.kwargs
    assert put["Key"] == "cfg/prod/app.json" and put["ContentType"] == "application/json"
    assert loader.load(put["Body"].decode(), "app.json") == fixture("app.xml")[1]


def test_s3_object_named_by_the_datasource_says_the_format():
    sink = _s3(settings={"file_pattern": "{env}.properties"})
    _s3_write(sink, "app.yaml")
    put = sink._s3.put_object.call_args.kwargs
    assert put["Key"] == "cfg/prod/prod.properties" and b"database.host=mongo.prod.internal" in put["Body"]


def test_s3_without_the_file_strategy_still_writes_config_json():
    from app.pipeline.sinks.s3 import S3Sink
    from app.pipeline.sinks.settings import Settings
    sink = S3Sink(settings=Settings.of("s3", bucket="b", prefix="cfg"))
    text, tree, _ = fixture("app.yaml")
    sink._write(flat_data=formats.flat(tree), data=tree, source_text=text, file_path="app.yaml", sync_mode=UPSERT, labels=LABELS, **WRITE)
    assert sink._s3.put_object.call_args.kwargs["Key"] == "cfg/prod/config.json"


def test_s3_reads_every_object_of_the_environment_back_by_its_own_format():
    sink = _s3()
    held = {"cfg/prod/app.yaml": (FIXTURES / "app.yaml").read_bytes(), "cfg/prod/other.json": b'{"extra": {"k": "v"}}',
            "cfg/prod/notes.txt": b"not a config", "cfg/prod/sub/x.yaml": b"deep: 1"}
    sink._s3.list_objects_v2.return_value = {"Contents": [{"Key": k} for k in held], "IsTruncated": False}
    sink._s3.get_object.side_effect = lambda Bucket, Key: {"Body": MagicMock(read=lambda: held[Key])}
    got = sink.read_values(["database.host", "extra.k", "deep"], "ds", "prod")
    assert got == {"database.host": "mongo.prod.internal", "extra.k": "v"}


def test_s3_leaves_an_object_alone_that_already_holds_the_file():
    sink = _s3()
    text = (FIXTURES / "app.yaml").read_bytes()
    sink._s3.get_object.return_value = {"Body": MagicMock(read=lambda: text)}
    out = _s3_write(sink, "app.yaml", SMART)
    assert out["written"] == 0 and out["unchanged"] == 10
    sink._s3.put_object.assert_not_called()


def _linux(tmp_path, file_name, **rule):
    from app.pipeline.sinks.linux_server import LinuxServerSink
    from app.pipeline.sinks.settings import Settings
    sink = LinuxServerSink(settings=Settings.of("linux_server", file_paths=[str(tmp_path / file_name)], update_strategy="STRATEGY_FILE"),
                           rule_config={"env_as_filename": False, **rule})
    return sink


def _linux_write(sink, name, mode=UPSERT, changed=None):
    text, tree, _ = fixture(name)
    data = changed or tree
    return sink._write(flat_data=formats.flat(data), data=data, source_text=text, file_path=name, sync_mode=mode, labels=LABELS, **WRITE)


@pytest.mark.parametrize("name", [n for n in BASE.values() if n != "app.env"])
def test_a_linux_server_file_is_the_file_as_it_is_in_git_when_its_path_says_its_format(tmp_path, name):
    sink = _linux(tmp_path, name)
    _linux_write(sink, name)
    assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes()


def test_a_linux_server_path_that_says_another_format_converts_the_file(tmp_path):
    sink = _linux(tmp_path, "config.json")
    _linux_write(sink, "app.yaml")
    assert loader.load((tmp_path / "config.json").read_text(), "config.json") == fixture("app.yaml")[1]


def test_a_linux_server_file_that_was_edited_by_hand_is_drift_and_is_put_back(tmp_path):
    sink = _linux(tmp_path, "app.toml")
    _linux_write(sink, "app.toml")
    target = tmp_path / "app.toml"
    target.write_text(target.read_text().replace("27017", "9999"))
    assert sink.read_values(["database.port"], "ds", "prod") == {"database.port": "9999"}
    out = _linux_write(sink, "app.toml", SMART)
    assert out["written"] == 10 and target.read_bytes() == (FIXTURES / "app.toml").read_bytes()


def test_a_linux_server_file_keeps_its_crlf_line_endings(tmp_path):
    sink = _linux(tmp_path, "app.yaml")
    text = "a: 1\r\nb: 2\r\n"
    tree = loader.load(text, "app.yaml")
    sink._write(flat_data=formats.flat(tree), data=tree, source_text=text, file_path="app.yaml", sync_mode=UPSERT, labels=LABELS, **WRITE)
    assert (tmp_path / "app.yaml").read_bytes() == b"a: 1\r\nb: 2\r\n"


# ── file_format and the file's name must agree ────────────────────────────────

def _checked(monkeypatch, rule, datasource="s3", **settings):
    """What the config validator says about a FILE rule, for a datasource with these settings."""
    from app import config_validator
    from app.pipeline.sinks import settings as sink_settings

    monkeypatch.setattr(sink_settings, "for_rule", lambda r, d: sink_settings.Settings.of(datasource, **settings))
    result = config_validator.Result()
    config_validator._file_format_against_name(rule, datasource, "bundle.json: the gitea/s3 rule", result)
    return result.errors


def test_a_file_name_that_says_another_format_than_file_format_is_refused_when_the_rule_loads(monkeypatch):
    errors = _checked(monkeypatch, {"file_format": "yaml"}, bucket="b", update_strategy="STRATEGY_FILE", file_pattern="{env}.json")
    assert len(errors) == 1 and "file_format is yaml" in errors[0] and "{env}.json says json" in errors[0]


def test_a_name_and_a_format_that_agree_are_fine_even_by_another_spelling(monkeypatch):
    assert _checked(monkeypatch, {"file_format": "yml"}, bucket="b", update_strategy="STRATEGY_FILE", file_pattern="x.yaml") == []
    assert _checked(monkeypatch, {"file_format": "json"}, bucket="b", update_strategy="STRATEGY_FILE", file_pattern="config") == []


def test_a_linux_server_path_is_the_name_too(monkeypatch):
    errors = _checked(monkeypatch, {"file_format": "yaml"}, datasource="linux_server", file_paths=["/etc/app/config.env"],
                      update_strategy="STRATEGY_FILE")
    assert len(errors) == 1 and "says env" in errors[0]


def test_without_the_file_strategy_file_format_keeps_its_old_meaning(monkeypatch):
    assert _checked(monkeypatch, {"file_format": "yaml"}, datasource="linux_server", file_paths=["/etc/app/config.env"]) == []


def test_a_file_format_that_is_not_a_format_is_an_error(monkeypatch):
    errors = _checked(monkeypatch, {"file_format": "docx"}, bucket="b", update_strategy="STRATEGY_FILE")
    assert len(errors) == 1 and "not a format varTrack writes" in errors[0]


def test_the_writer_refuses_the_same_disagreement_for_a_rule_that_never_met_the_validator():
    text, tree, _ = fixture("app.yaml")
    with pytest.raises(Unrenderable, match="file_format is yaml, but the file name x.json says json"):
        file_output(source_text=text, file_path="app.yaml", data=tree, flat_data=formats.flat(tree), file_format="yaml", given_name="x.json")


def test_the_indentation_copied_from_the_source_is_bounded():
    wide = "a:\n" + " " * 40 + "b: 1\n"
    out = formats.render("json", {"a": {"b": 1}}, source_family="yaml", source_text=wide)
    first = out.splitlines()[1]
    assert first.startswith(" " * 8 + '"a"') and not first.startswith(" " * 9)     # 40 columns in the source, 8 in the output


# ── A secret reference in a file, in every format ─────────────────────────────

SECRET = "s3cr3t-from-vault"


def _file_with_the_secret(name: str, **options):
    """What a FILE sink is given for e2e/configs/formats/secret/<name>: the ETL's steps (flatten, resolve the secret
    the schema marks, the file's shape) on the file, and file_output on that."""
    import json
    from unittest.mock import patch
    from app.pipeline import transformer
    from app.pipeline.secret_resolver import resolve_secrets

    text = (FIXTURES / "secret" / name).read_text(encoding="utf-8")
    parsed = loader.load(text, name)
    flat = transformer.transform(content=json.dumps(parsed), file_path=name, variables_map={}, env="default", branch="",
                                 repo="", pr_number=None, tag="", root_key="", parsed=parsed)
    field = "config.database.password" if name == "app.xml" else "database.password"
    client = MagicMock()
    client.get_secret.return_value = SECRET
    managers = {"default": {"vault_addr": "http://vault", "mount_point": "secret", "path_prefix": "x", "token": "t"}}
    with patch("app.pipeline.secret_resolver.VaultClient", MagicMock(return_value=client)):
        flat = resolve_secrets(flat, {field: None}, managers)
    client.get_secret.assert_called_once_with("e2e-formats", key="password")      # the reference's own path and key
    data = transformer.file_shape(parsed, flat, {})
    return text, file_output(source_text=text, file_path=name, data=data, flat_data=flat, **options)


@pytest.mark.parametrize("name", BASE.values())
def test_a_secret_reference_is_replaced_in_a_file_of_every_format_and_it_still_reads(name):
    text, out = _file_with_the_secret(name)
    stored = out.content.decode("utf-8")
    assert not out.copied and "@secret(" not in stored
    flat = formats.flat(formats.clean_hcl(loader.load(stored, name)) if name == "app.hcl" else loader.load(stored, name))
    prefix = "config." if name == "app.xml" else ""
    assert flat == {f"{prefix}app.name": "demo-app", f"{prefix}database.host": "mongo.prod.internal",
                    f"{prefix}database.password": SECRET}


def test_the_secret_in_an_hcl_file_keeps_the_quotes_of_the_string_it_replaced():
    _, out = _file_with_the_secret("app.hcl")
    assert f'password = "{SECRET}"' in out.content.decode("utf-8")
