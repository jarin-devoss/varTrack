"""
app/monitoring
───────────────
Monitoring package for the VarTrack orchestrator service.

Public API
──────────
    init(version, commit, monitoring_config) → OrchestratorMetrics
        Configure structured JSON logging, create the singleton metrics
        registry, start monitoring backends, and wire Celery signals.
        Call once at startup from app/api/app.py lifespan and from
        app/worker/celery.py after the Celery app is created.

    get_metrics() → OrchestratorMetrics | None
        Return the singleton, or None if init() has not been called yet.
        Safe to call from any module — returns None rather than raising.

Sub-modules
───────────
    config.py      — MonitoringConfig, PrometheusConfig, JaegerConfig, OTelConfig
    backends.py    — re-export shim: Backend ABC + build_backends() factory
    base.py        — Backend ABC
    prometheus.py  — PrometheusBackend (scrape server + pushgateway loop)
    jaeger.py      — JaegerBackend (OTLP/gRPC or OTLP/HTTP span exporter)
    otel.py        — OTelBackend (OTLP traces + metrics)
    elk.py         — ELKBackend + _ESClient + _LogstashWriter
    _utils.py      — shared TLS context + OTel resource/sampler builders
    logger.py      — configure_logging(): JSON structured logs via structlog
    tracer.py      — start() / start_webhook_span() / start_etl_span(): lightweight spans
    metrics.py     — OrchestratorMetrics: all Prometheus instruments, non-global registry
    instrument.py  — MetricsMiddleware: FastAPI ASGI HTTP instrumentation
    celery_signals.py — Celery task signal hooks
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from app.monitoring.metrics import OrchestratorMetrics

if TYPE_CHECKING:
    from app.monitoring.config import MonitoringConfig
    from app.monitoring.backends import Backend

logger = logging.getLogger(__name__)

_metrics:  OrchestratorMetrics | None = None
_backends: list[Backend]              = []
_init_lock = threading.Lock()


def init(
    version: str = "dev",
    commit:  str = "unknown",
    monitoring_config: MonitoringConfig | None = None,
) -> OrchestratorMetrics:
    """
    Initialise the full monitoring subsystem.

    Steps:
      1. Configure structured JSON logging
      2. Create OrchestratorMetrics with non-global registry
      3. Build and start monitoring backends:
           PrometheusBackend — scrape endpoint + pushgateway loop
           JaegerBackend     — OTLP trace exporter
           OTelBackend       — OTLP metrics + traces
           ELKBackend        — ES Bulk API / Logstash log shipping
      4. Wire Celery task signal hooks

    Idempotent — subsequent calls return the existing instance without
    re-registering metrics (safe to call from both API and worker processes).

    Args:
        version:           Application version string (e.g. "0.2.0").
        commit:            Git commit SHA (e.g. "a1b2c3d").
        monitoring_config: Explicit config; if None, loaded from env vars
                           via MonitoringConfig.from_env().

    Returns:
        The singleton OrchestratorMetrics instance.
    """
    global _metrics, _backends

    with _init_lock:
        if _metrics is not None:
            return _metrics

        # 1. Structured JSON logging first.
        try:
            from app.monitoring.logger import configure_logging
            configure_logging()
        except Exception:
            pass  # stdlib logging still works; never block startup

        import sys
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        # 2. Prometheus metrics registry.
        _metrics = OrchestratorMetrics()
        _metrics.set_build_info(version, commit, python_version)

        # 3. Monitoring backends (Prometheus scrape, Jaeger, OTel).
        try:
            from app.monitoring.config import MonitoringConfig as _MonCfg
            from app.monitoring.backends import build_backends
            cfg = monitoring_config or _MonCfg.from_env()
            _backends = build_backends(cfg, version, commit, _metrics._registry)
        except Exception:
            logger.warning("monitoring: backend startup failed", exc_info=True)

        # 4. Celery task signal hooks.
        try:
            from app.monitoring.celery_signals import connect_signals
            connect_signals()
        except Exception:
            logger.warning("monitoring: failed to connect Celery signals", exc_info=True)

        logger.info(
            "monitoring: initialised version=%s commit=%s python=%s backends=%s",
            version, commit, python_version,
            [b.name() for b in _backends],
        )

    return _metrics


def get_metrics() -> OrchestratorMetrics | None:
    """Return the singleton or None if init() has not been called yet."""
    return _metrics


def shutdown() -> None:
    """
    Flush and stop all monitoring backends.
    Call from the FastAPI lifespan shutdown hook.
    """
    for backend in _backends:
        try:
            backend.shutdown()
        except Exception:
            logger.warning("monitoring: shutdown error for %s", backend.name(), exc_info=True)


__all__ = [
    "OrchestratorMetrics", "init", "get_metrics", "shutdown",
    "configure_logging",
]
