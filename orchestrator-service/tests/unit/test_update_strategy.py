"""
tests/unit/test_update_strategy.py

A datasource's update_strategy decides the shape a sink stores, from what the
ETL hands it: the flat keys, and the file in the shape it parsed to.

  STRATEGY_KEY_VALUE  one entry per flat key
  STRATEGY_DOCUMENT   one document per file, always nested
  STRATEGY_FILE       one document per file, as it parsed (nested, or flat
                      for .env / .properties)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.models import PayloadContext
from app.pipeline.sinks.base import update_strategy
from app.pipeline.sinks.s3 import S3Sink
from app.pipeline.sinks.strategies.mongo import DocumentStrategy, FileStrategy, KeyValueStrategy, get_strategy
from app.pipeline.sinks.zookeeper import ZookeeperSink
from app.pipeline.transformer import _py_flatten, file_shape, transform, unflatten


class FakeCollection:
    """The slice of a pymongo collection the strategies use: equality, $exists and $in filters."""

    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    @staticmethod
    def _matches(doc, flt):
        for field, cond in flt.items():
            if isinstance(cond, dict) and "$exists" in cond:
                if (field in doc) != cond["$exists"]:
                    return False
            elif isinstance(cond, dict) and "$in" in cond:
                if doc.get(field) not in cond["$in"]:
                    return False
            elif doc.get(field) != cond:
                return False
        return True

    def find(self, flt, projection=None):
        return [dict(d) for d in self.docs if self._matches(d, flt)]

    def find_one(self, flt, projection=None):
        return next(iter(self.find(flt)), None)

    def replace_one(self, flt, doc, upsert=False):
        for i, d in enumerate(self.docs):
            if self._matches(d, flt):
                self.docs[i] = dict(doc)
                return
        if upsert:
            self.docs.append(dict(doc))

    def delete_many(self, flt):
        self.docs = [d for d in self.docs if not self._matches(d, flt)]


SCOPE = {"_vt_tenant": "acme", "_vt_datasource": "mongo", "_vt_env": "production"}
TREE = {"api": {"rate_limit": 6000, "regions": ["eu", "us"]}, "debug": False}
TREE_FLAT = _py_flatten(TREE)  # {"api.rate_limit": "6000", "api.regions.0": "eu", …, "debug": "false"}
PROPERTIES = {"db.host": "x", "db.port": "5432"}  # a .properties file parses to flat keys


# ── update_strategy, the proto enum ───────────────────────────────────────────

@pytest.mark.parametrize("value,name", [
    (None, "STRATEGY_UNSPECIFIED"), ("", "STRATEGY_UNSPECIFIED"), (0, "STRATEGY_UNSPECIFIED"),
    ("STRATEGY_FILE", "STRATEGY_FILE"), ("strategy_document", "STRATEGY_DOCUMENT"), (1, "STRATEGY_KEY_VALUE"),
])
def test_update_strategy_reads_names_and_numbers(value, name):
    assert update_strategy(value) == name


def test_an_unknown_update_strategy_is_an_error():
    with pytest.raises(ValueError):
        update_strategy("STRATEGY_BLOB")
    with pytest.raises(ValueError, match="STRATEGY_BLOB"):
        get_strategy("STRATEGY_BLOB")


@pytest.mark.parametrize("value,strategy", [
    (None, KeyValueStrategy), ("STRATEGY_KEY_VALUE", KeyValueStrategy),
    ("STRATEGY_DOCUMENT", DocumentStrategy), ("STRATEGY_FILE", FileStrategy), (2, DocumentStrategy),
])
def test_mongo_strategy_follows_update_strategy(value, strategy):
    assert type(get_strategy(value)) is strategy


# ── unflatten ─────────────────────────────────────────────────────────────────

def test_unflatten_is_the_inverse_of_flatten():
    nested = {"database": {"host": "db", "port": "5432"}, "name": "x"}
    assert unflatten(_py_flatten(nested)) == nested


def test_a_key_that_is_a_value_and_a_parent_has_no_nested_form():
    with pytest.raises(ValueError, match="'a' is both a value and the parent"):
        unflatten({"a": "1", "a.b": "2"})


# ── the file's shape ──────────────────────────────────────────────────────────

def test_a_flat_file_is_its_flat_keys():
    assert file_shape(PROPERTIES, PROPERTIES, {}) is PROPERTIES


def test_a_tree_keeps_its_types_and_takes_renames_and_secrets():
    parsed = {"api": {"rate_limit": 6000, "secret": "@secret(x)"}, "items": [{"n": "a"}], "lit": {"a.b": 1}}
    flat = _py_flatten(parsed)
    flat["api.secret"] = "s3cret"                       # resolved from Vault
    flat["RATE"] = flat.pop("api.rate_limit")           # variables_map rename
    assert file_shape(parsed, flat, {"RATE": "api.rate_limit"}) == {
        "api": {"secret": "s3cret"}, "items": [{"n": "a"}], "lit": {"a.b": 1}, "RATE": 6000,
    }
    assert parsed["api"]["secret"] == "@secret(x)"      # the parsed file is left as it was


@pytest.mark.parametrize("name,content,flat", [
    ("app.env", "DB_HOST=x\nDB_PORT=5432\n", {"DB_HOST": "x", "DB_PORT": "5432"}),
    ("app.properties", "db.host=x\n", {"db.host": "x"}),
    ("app.toml", "[db]\nhost = 'x'\nport = 5432\n", {"db.host": "x", "db.port": "5432"}),
    ("app.ini", "[db]\nhost = x\n", {"db.host": "x"}),
])
def test_transform_flattens_the_data_already_parsed(name, content, flat):
    # stage_etl hands transform() the parsed, sliced data; re-parsing its JSON
    # as the file's own format broke every format but YAML and JSON.
    from app.parsers.loader import load
    parsed = load(content, name)
    assert transform(content=json.dumps(parsed), file_path=name, variables_map={}, env="e", parsed=parsed) == flat


def test_stage_etl_records_the_file_in_the_shape_it_parsed_to():
    from app.pipeline.stage_etl import run_stage_etl

    ctx = PayloadContext(platform="github", repo_url="https://github.com/o/r", ref="refs/heads/main",
                         branch="main", commit_sha="abc", tag=None, pr_number=None,
                         rule_config={"root_key": ""}, parsed_payload={})
    schema = MagicMock()
    schema.tenant_schema_root.return_value = Path("/nonexistent")
    validator = MagicMock()
    validator._find_schema.return_value = None
    files = [("app.yaml", "api:\n  rate_limit: 6000\n"), ("app.env", "DB_HOST=x\n")]
    with (
        patch("app.pipeline.stage_etl.collect_files", return_value=files),
        patch("app.schema_registry.manager.get_schema_manager", return_value=schema),
        patch("app.pipeline.validator.Validator", return_value=validator),
    ):
        result = run_stage_etl(ctx, datasource="mongo", tenant_id="acme")
    yaml_file, env_file = result.files
    assert (yaml_file.flat_data, yaml_file.data) == ({"api.rate_limit": "6000"}, {"api": {"rate_limit": 6000}})
    assert (env_file.flat_data, env_file.data) == ({"DB_HOST": "x"}, {"DB_HOST": "x"})


# ── MongoDB ───────────────────────────────────────────────────────────────────

def test_document_stores_the_tree_and_reads_it_back_as_the_flat_keys():
    col = FakeCollection()
    DocumentStrategy().upsert(col, TREE_FLAT, SCOPE, "abc", "app.yaml", {}, data=TREE)
    assert len(col.docs) == 1 and col.docs[0]["data"] == TREE
    assert DocumentStrategy().read_values(col, list(TREE_FLAT), SCOPE) == TREE_FLAT


def test_document_nests_a_flat_file():
    col = FakeCollection()
    DocumentStrategy().upsert(col, PROPERTIES, SCOPE, "abc", "app.properties", {}, data=PROPERTIES)
    assert col.docs[0]["data"] == {"db": {"host": "x", "port": "5432"}}


def test_file_stores_the_file_as_it_parsed():
    col = FakeCollection()
    FileStrategy().upsert(col, TREE_FLAT, SCOPE, "abc", "app.yaml", {}, data=TREE)
    FileStrategy().upsert(col, PROPERTIES, SCOPE, "abc", "app.properties", {}, data=PROPERTIES)
    by_file = {d["_vt_file"]: d["data"] for d in col.docs}
    assert by_file == {"app.yaml": TREE, "app.properties": PROPERTIES}
    assert FileStrategy().read_values(col, ["db.port", "api.rate_limit"], SCOPE) == {
        "db.port": "5432", "api.rate_limit": "6000",
    }


def test_a_rewrite_drops_keys_removed_from_git_and_the_per_key_documents_before_it():
    col = FakeCollection([{**SCOPE, "_vt_file": "app.yaml", "key": "old", "value": "1"},
                          {**SCOPE, "_vt_file": "other.yaml", "key": "x", "value": "1"}])
    DocumentStrategy().upsert(col, TREE_FLAT, SCOPE, "abc", "app.yaml", {}, data=TREE)
    DocumentStrategy().upsert(col, {"debug": "false"}, SCOPE, "def", "app.yaml", {}, data={"debug": False})
    assert sorted((d["_vt_file"], d.get("data")) for d in col.docs) == [
        ("app.yaml", {"debug": False}), ("other.yaml", None),  # another file's documents are left alone
    ]


def test_key_value_reads_one_document_per_key():
    col = FakeCollection([{**SCOPE, "key": k, "value": v} for k, v in PROPERTIES.items()])
    assert KeyValueStrategy().read_values(col, ["db.host"], SCOPE) == {"db.host": "x"}


# ── S3 ────────────────────────────────────────────────────────────────────────

def _s3(**cfg) -> S3Sink:
    return S3Sink(rule_config={"s3_bucket": "b", **cfg})


@pytest.mark.parametrize("cfg,flat,data,expected", [
    ({"update_strategy": "STRATEGY_DOCUMENT"}, TREE_FLAT, TREE, TREE),
    ({"update_strategy": "STRATEGY_DOCUMENT"}, PROPERTIES, PROPERTIES, {"db": {"host": "x", "port": "5432"}}),
    ({"update_strategy": "STRATEGY_FILE"}, TREE_FLAT, TREE, TREE),
    ({"update_strategy": "STRATEGY_FILE"}, PROPERTIES, PROPERTIES, PROPERTIES),
    ({}, TREE_FLAT, TREE, TREE_FLAT),  # unset: config.json with the flat keys, as before
])
def test_s3_config_json_follows_update_strategy(cfg, flat, data, expected):
    sink = _s3(**cfg)
    sink._put_single(flat, sink._document(flat, data), "production", "")
    body = sink._s3.put_object.call_args.kwargs["Body"]
    assert json.loads(body) == expected


@pytest.mark.parametrize("cfg,per_key", [
    ({"update_strategy": "STRATEGY_KEY_VALUE"}, True), ({"s3_one_file_per_key": True}, True),
    ({"update_strategy": "STRATEGY_FILE", "s3_one_file_per_key": True}, False), ({}, False),
])
def test_s3_writes_one_object_per_key_for_key_value(cfg, per_key):
    assert _s3(**cfg)._one_per_key is per_key


# ── ZooKeeper ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("strategy", ["STRATEGY_DOCUMENT", "STRATEGY_FILE"])
def test_zookeeper_stores_flat_keys_only(strategy):
    with pytest.raises(ValueError, match="one znode per key"):
        ZookeeperSink(rule_config={"update_strategy": strategy})


def test_zookeeper_takes_key_value():
    ZookeeperSink(rule_config={"update_strategy": "STRATEGY_KEY_VALUE"})
