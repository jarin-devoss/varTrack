"""
app/pipeline/sinks/__init__.py
───────────────────────────────
Eagerly imports every built-in sink so they register themselves in
BaseSink._registry before the first BaseSink.create() call.

To add a new sink:
  1. Create app/pipeline/sinks/<kind>.py
  2. Import the class below and add it to SINK_REGISTRY.
"""
from __future__ import annotations

from app.pipeline.sinks.base import BaseSink      # noqa: F401

from app.pipeline.sinks.mongo        import MongoSink        # noqa: F401
from app.pipeline.sinks.redis        import RedisSink        # noqa: F401
from app.pipeline.sinks.s3           import S3Sink           # noqa: F401
from app.pipeline.sinks.zookeeper    import ZookeeperSink    # noqa: F401
from app.pipeline.sinks.configmap    import ConfigmapSink    # noqa: F401
from app.pipeline.sinks.linux_server import LinuxServerSink  # noqa: F401
from app.pipeline.sinks.vercel       import VercelSink       # noqa: F401
from app.pipeline.sinks.helm         import HelmSink         # noqa: F401

# ── Sink registry: datasource name → sink class ───────────────────────────────

SINK_REGISTRY: dict[str, type[BaseSink]] = {
    "mongo":        MongoSink,
    "redis":        RedisSink,
    "s3":           S3Sink,
    "configmap":    ConfigmapSink,
    "helm":         HelmSink,
    "linux_server": LinuxServerSink,
    "vercel":       VercelSink,
    "zookeeper":    ZookeeperSink,
}


def create_sink(datasource: str, rule_config: dict, tenant_id: str) -> BaseSink:
    """
    Instantiate the right sink class for the given datasource name.

    The datasource comes from the webhook URL path:
        POST /v1/webhooks/{datasource}
    Maps directly to a sink implementation:
        "mongo"        → MongoSink
        "redis"        → RedisSink
        "s3"           → S3Sink
        "configmap"    → ConfigmapSink
        "helm"         → HelmSink
        "linux_server" → LinuxServerSink
        "vercel"       → VercelSink
        "zookeeper"    → ZookeeperSink

    Parameters
    ----------
    datasource:
        Datasource identifier from the webhook URL (case-insensitive,
        hyphens normalised to underscores).
    rule_config:
        Full rule config dict from the schema bundle.
    tenant_id:
        Tenant identifier for the sink's label/namespace logic.

    Raises
    ------
    ValueError
        If datasource does not map to any registered sink.
    """
    # Datasource names follow the "type-tag" convention from config.cue:
    #   "mongo-primary" → type="mongo", tag="primary"
    #   "redis-dr"      → type="redis", tag="dr"
    # First try an exact match, then fall back to the type prefix.
    key = datasource.lower().replace("-", "_")
    cls = SINK_REGISTRY.get(key)
    if cls is None:
        # Strip tag suffix: "mongo_primary" → "mongo"
        type_prefix = key.split("_")[0]
        cls = SINK_REGISTRY.get(type_prefix)
    if cls is None:
        raise ValueError(
            f"No sink registered for datasource {datasource!r}. "
            f"Known datasources: {sorted(SINK_REGISTRY)}"
        )
    return cls(rule_config=rule_config, tenant_id=tenant_id)
