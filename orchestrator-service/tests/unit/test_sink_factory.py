"""
tests/unit/test_sink_factory.py

Unit tests for app/pipeline/sinks create_sink() factory and SINK_REGISTRY.

Key invariants:
  - Each known datasource name maps to the correct sink class
  - Datasource name is case-insensitive and hyphen-tolerant
  - Unknown datasource raises ValueError with a helpful message
  - create_sink() builds the sink from its datasource's block in config.cue, the rule and the tenant
  - BaseSink.delete_file_data default no-op: logs warning, returns 0, no crash

External heavy deps (pymongo, redis, boto3, kubernetes, kazoo, hvac, paramiko)
are already stubbed in conftest.py — do NOT re-stub internal app modules here.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.pipeline.sinks import SINK_REGISTRY, create_sink
from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.mongo import MongoSink
from app.pipeline.sinks.redis import RedisSink
from app.pipeline.sinks.s3 import S3Sink


_RULE_CONFIG_MONGO = {"key_format": "dot"}

# config.cue's datasource blocks: what a sink connects with.
_BUNDLE = {"datasources": [
    {"mongo": {"endpoint": "mongodb://localhost:27017", "database": "test"}},
    {"redis": {"hosts": ["localhost:6379"]}},
    {"s3": {"bucket": "my-bucket", "region": "us-east-1"}},
    {"linux_server": {"file_paths": ["/etc/app/config.env"], "host": "host"}},
]}


class TestSinkRegistry(unittest.TestCase):

    def setUp(self):
        patcher = patch("app.pipeline.datasource_settings._bundle", return_value=_BUNDLE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registry_contains_all_known_datasources(self):
        for name in ("mongo", "redis", "s3", "configmap", "helm",
                     "linux_server", "zookeeper"):
            self.assertIn(name, SINK_REGISTRY, f"SINK_REGISTRY missing {name!r}")

    def test_create_sink_mongo_returns_mongosink(self):
        sink = create_sink("mongo", _RULE_CONFIG_MONGO, "acme")
        self.assertIsInstance(sink, MongoSink)

    def test_create_sink_redis_returns_redissink(self):
        sink = create_sink("redis", {}, "acme")
        self.assertIsInstance(sink, RedisSink)

    def test_create_sink_s3_returns_s3sink(self):
        sink = create_sink("s3", {}, "acme")
        self.assertIsInstance(sink, S3Sink)
        self.assertEqual(sink._bucket, "my-bucket")

    def test_a_rules_copy_of_a_datasource_field_does_not_reach_the_sink(self):
        sink = create_sink("s3", {"bucket": "evil", "s3_bucket": "evil"}, "acme")
        self.assertEqual(sink._bucket, "my-bucket")

    def test_create_sink_case_insensitive(self):
        sink_lower = create_sink("mongo", _RULE_CONFIG_MONGO, "acme")
        sink_upper = create_sink("MONGO", _RULE_CONFIG_MONGO, "acme")
        self.assertIsInstance(sink_lower, MongoSink)
        self.assertIsInstance(sink_upper, MongoSink)

    def test_create_sink_hyphen_normalised_to_underscore(self):
        sink = create_sink("linux-server", {}, "acme")
        from app.pipeline.sinks.linux_server import LinuxServerSink
        self.assertIsInstance(sink, LinuxServerSink)

    def test_create_sink_unknown_raises_valueerror(self):
        with self.assertRaises(ValueError) as cm:
            create_sink("nonexistent_db", {}, "acme")
        msg = str(cm.exception)
        self.assertIn("nonexistent_db", msg)
        self.assertIn("Known datasources", msg)

    def test_create_sink_passes_rule_config_and_tenant(self):
        sink = create_sink("mongo", _RULE_CONFIG_MONGO, "tenant42")
        self.assertIsNotNone(sink)
        # The sink stores tenant_id
        self.assertEqual(sink._tenant_id, "tenant42")


class TestWhichSinksCanTakeAWriteBack(unittest.TestCase):
    """A sink says whether it can restore what a sync wrote (can_restore); one that cannot is never asked."""

    def _make_test_sink(self, suffix: str) -> BaseSink:
        """Create a minimal concrete BaseSink subclass."""
        class _TestSink(BaseSink):
            name = f"_test_{suffix}"

            def _write(self, **kwargs):
                return {"written": 0, "pruned": 0}

            def close(self):
                pass

        return _TestSink()

    def test_a_sink_cannot_take_a_write_back_unless_it_says_it_can(self):
        self.assertFalse(self._make_test_sink("default").can_restore)

    def test_asking_a_sink_that_cannot_is_an_error_not_a_quiet_zero(self):
        # It used to log a warning and return 0, and stage_sync then logged "rollback: deleted file=…".
        sink = self._make_test_sink("noop")
        with self.assertRaisesRegex(NotImplementedError, "cannot take back what a sync wrote"):
            sink.delete_file_data("redis", "staging", "app.yaml")

    def test_only_mongo_can_restore(self):
        self.assertTrue(MongoSink.can_restore)
        for kind, cls in SINK_REGISTRY.items():
            if kind != "mongo":
                self.assertFalse(cls.can_restore, f"{kind} keeps no snapshot, so it cannot restore")

    def test_mongo_sink_overrides_what_it_needs_to_restore(self):
        self.assertIsNot(MongoSink.delete_file_data, BaseSink.delete_file_data)
        self.assertTrue(callable(MongoSink.snapshot_file_data) and callable(MongoSink.restore_file_data))


if __name__ == "__main__":
    unittest.main()
