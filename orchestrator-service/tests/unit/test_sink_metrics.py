"""Sink metrics are named by the sink that made them; the orch_mongo_* names are Mongo's alone."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.monitoring.metrics import OrchestratorMetrics
from app.pipeline import stage_sync
from app.pipeline.models import SyncResult


@pytest.fixture
def metrics():
    return OrchestratorMetrics()


def sample(m, name, **labels):
    return m._registry.get_sample_value(name, labels) or 0


def test_a_redis_write_is_counted_as_redis_and_not_as_mongo(metrics):
    metrics.observe_sink_write("redis", "GIT_UPSERT_ALL", 0.02)
    metrics.inc_sink_written("redis", "cache", "GIT_UPSERT_ALL", 4)
    metrics.inc_sink_pruned("redis", "cache", 2)
    metrics.inc_sink_error("redis", "write")

    assert sample(metrics, "orch_sink_write_duration_seconds_count", sink="redis", sync_mode="GIT_UPSERT_ALL") == 1
    assert sample(metrics, "orch_sink_keys_written_total", sink="redis", datasource="cache") == 4
    assert sample(metrics, "orch_sink_keys_pruned_total", sink="redis", datasource="cache") == 2
    assert sample(metrics, "orch_sink_errors_total", sink="redis", operation="write") == 1
    # what used to be reported for every sink under Mongo's name
    assert sample(metrics, "orch_mongo_write_duration_seconds_count", sync_mode="GIT_UPSERT_ALL") == 0
    assert sample(metrics, "orch_mongo_documents_written_total", datasource="cache", sync_mode="GIT_UPSERT_ALL") == 0
    assert sample(metrics, "orch_mongo_documents_pruned_total", datasource="cache") == 0
    assert sample(metrics, "orch_mongo_errors_total", operation="write") == 0


def test_a_mongo_write_feeds_both_names_for_one_release(metrics):
    metrics.observe_sink_write("mongo", "GIT_SMART_REPAIR", 0.1)
    metrics.inc_sink_written("mongo", "docs", "GIT_SMART_REPAIR", 3)
    metrics.inc_sink_pruned("mongo", "docs", 1)
    metrics.inc_sink_error("mongo", "rollback")

    assert sample(metrics, "orch_sink_keys_written_total", sink="mongo", datasource="docs") == 3
    assert sample(metrics, "orch_mongo_documents_written_total", datasource="docs", sync_mode="GIT_SMART_REPAIR") == 3
    assert sample(metrics, "orch_mongo_write_duration_seconds_count", sync_mode="GIT_SMART_REPAIR") == 1
    assert sample(metrics, "orch_mongo_documents_pruned_total", datasource="docs") == 1
    assert sample(metrics, "orch_mongo_errors_total", operation="rollback") == 1


def test_nothing_written_is_not_counted(metrics):
    metrics.inc_sink_written("s3", "files", "GIT_UPSERT_ALL", 0)
    metrics.inc_sink_pruned("s3", "files", 0)
    assert sample(metrics, "orch_sink_keys_written_total", sink="s3", datasource="files") == 0


def test_the_kind_of_a_sink_is_the_datasource_kind_it_serves():
    from app.pipeline.sinks.linux_server import LinuxServerSink
    from app.pipeline.sinks.redis import RedisSink

    assert stage_sync._sink_kind(RedisSink.__new__(RedisSink)) == "redis"
    assert stage_sync._sink_kind(LinuxServerSink.__new__(LinuxServerSink)) == "linux_server"
    assert stage_sync._sink_kind(object()) == "object"


class _CannotRestore:
    can_restore = False


class _RestoreFails:
    can_restore = True

    def restore_file_data(self, *a):
        raise RuntimeError("store went away")


def _written(n):
    return [("ds", "prod", f"f{i}.yaml", [], SyncResult(file_path=f"f{i}.yaml", env="prod", status="ok", written=1))
            for i in range(n)]


def test_files_a_sink_cannot_take_back_are_counted(metrics):
    with patch.object(stage_sync, "_m", return_value=metrics):
        stage_sync._roll_back(_CannotRestore(), _written(2), failed_file="g.yaml", reason="boom")
    assert sample(metrics, "orch_sink_not_rolled_back_total", sink="_cannotrestore") == 2


def test_a_rollback_that_fails_is_an_error_and_a_file_left_written(metrics):
    with patch.object(stage_sync, "_m", return_value=metrics):
        stage_sync._roll_back(_RestoreFails(), _written(1), failed_file="g.yaml", reason="boom")
    assert sample(metrics, "orch_sink_errors_total", sink="_restorefails", operation="rollback") == 1
    assert sample(metrics, "orch_sink_not_rolled_back_total", sink="_restorefails") == 1


def test_a_metric_that_fails_never_fails_the_sync():
    class Broken:
        def inc_sink_not_rolled_back(self, sink):
            raise RuntimeError("registry gone")

    with patch.object(stage_sync, "_m", return_value=Broken()):
        stage_sync._roll_back(_CannotRestore(), _written(1), failed_file="g.yaml", reason="boom")
