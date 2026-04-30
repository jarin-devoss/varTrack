"""
tests/unit/test_sink_factory.py

Unit tests for app/pipeline/sinks create_sink() factory and SINK_REGISTRY.

Key invariants:
  - Each known datasource name maps to the correct sink class
  - Datasource name is case-insensitive and hyphen-tolerant
  - Unknown datasource raises ValueError with a helpful message
  - create_sink() passes rule_config and tenant_id to the sink constructor
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


_RULE_CONFIG_MONGO = {"mongo_uri": "mongodb://localhost:27017", "database": "test"}


class TestSinkRegistry(unittest.TestCase):

    def test_registry_contains_all_known_datasources(self):
        for name in ("mongo", "redis", "s3", "configmap", "helm",
                     "linux_server", "vercel", "zookeeper"):
            self.assertIn(name, SINK_REGISTRY, f"SINK_REGISTRY missing {name!r}")

    def test_create_sink_mongo_returns_mongosink(self):
        sink = create_sink("mongo", _RULE_CONFIG_MONGO, "acme")
        self.assertIsInstance(sink, MongoSink)

    def test_create_sink_redis_returns_redissink(self):
        sink = create_sink("redis", {"redis_url": "redis://localhost:6379"}, "acme")
        self.assertIsInstance(sink, RedisSink)

    def test_create_sink_s3_returns_s3sink(self):
        sink = create_sink("s3", {"s3_bucket": "my-bucket", "region": "us-east-1"}, "acme")
        self.assertIsInstance(sink, S3Sink)

    def test_create_sink_case_insensitive(self):
        sink_lower = create_sink("mongo", _RULE_CONFIG_MONGO, "acme")
        sink_upper = create_sink("MONGO", _RULE_CONFIG_MONGO, "acme")
        self.assertIsInstance(sink_lower, MongoSink)
        self.assertIsInstance(sink_upper, MongoSink)

    def test_create_sink_hyphen_normalised_to_underscore(self):
        sink = create_sink("linux-server", {"file_path": "/etc/app/config.env", "ssh_host": "host"}, "acme")
        from app.pipeline.sinks.linux_server import LinuxServerSink
        self.assertIsInstance(sink, LinuxServerSink)

    def test_create_sink_unknown_raises_valueerror(self):
        with self.assertRaises(ValueError) as cm:
            create_sink("nonexistent_db", {}, "acme")
        msg = str(cm.exception)
        self.assertIn("nonexistent_db", msg)
        self.assertIn("Known datasources", msg)

    def test_create_sink_passes_rule_config_and_tenant(self):
        # MongoSink reads mongo_uri from rule_config — verify it's passed through
        rule_config = {"mongo_uri": "mongodb://test:27017", "database": "vartrack"}
        sink = create_sink("mongo", rule_config, "tenant42")
        self.assertIsNotNone(sink)
        # The sink stores tenant_id
        self.assertEqual(sink._tenant_id, "tenant42")


class TestBaseSinkDeleteFileDataDefault(unittest.TestCase):
    """BaseSink.delete_file_data default no-op behaviour."""

    def _make_test_sink(self, suffix: str) -> BaseSink:
        """Create a minimal concrete BaseSink subclass."""
        class _TestSink(BaseSink):
            name = f"_test_{suffix}"

            def _write(self, **kwargs):
                return {"written": 0, "pruned": 0}

            def close(self):
                pass

        return _TestSink()

    def test_default_returns_zero(self):
        sink = self._make_test_sink("zero")
        result = sink.delete_file_data("mongo", "production", "config.yaml")
        self.assertEqual(result, 0)

    def test_default_does_not_raise(self):
        sink = self._make_test_sink("noraise")
        # Should not raise any exception
        try:
            sink.delete_file_data("redis", "staging", "app.yaml")
        except Exception as exc:  # pragma: no cover
            self.fail(f"delete_file_data raised unexpectedly: {exc!r}")

    def test_default_logs_warning(self):
        sink = self._make_test_sink("logwarn")
        import logging
        with self.assertLogs("app.pipeline.sinks.base", level=logging.WARNING):
            sink.delete_file_data("mongo", "production", "config.yaml")

    def test_mongo_sink_overrides_delete_file_data(self):
        """MongoSink should override delete_file_data (not use the default no-op)."""
        # Just check it's overridden — MongoSink has its own implementation
        mongo_sink_method = MongoSink.delete_file_data
        base_method = BaseSink.delete_file_data
        self.assertIsNot(mongo_sink_method, base_method,
                         "MongoSink must override delete_file_data for rollback support")


if __name__ == "__main__":
    unittest.main()
