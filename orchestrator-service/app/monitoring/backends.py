"""
app/monitoring/backends.py
───────────────────────────
Backward-compatible entry point.

Re-exports Backend and build_backends so existing call-sites
(app/monitoring/__init__.py) continue to work without changes:

    from app.monitoring.backends import Backend        # type-check
    from app.monitoring.backends import build_backends  # runtime

The actual implementations now live in their own modules:
    base.py        — Backend ABC
    prometheus.py  — PrometheusBackend
    jaeger.py      — JaegerBackend
    otel.py        — OTelBackend
    elk.py         — ELKBackend
    _utils.py      — shared TLS + OTel helpers
"""
from __future__ import annotations

from app.monitoring.base import Backend
from app.monitoring.prometheus import PrometheusBackend
from app.monitoring.jaeger import JaegerBackend
from app.monitoring.otel import OTelBackend
from app.monitoring.elk import ELKBackend
from app.monitoring.config import MonitoringConfig

__all__ = ["Backend", "build_backends"]


def build_backends(
    cfg: MonitoringConfig,
    version: str,
    commit: str,
    registry,
) -> list[Backend]:
    """
    Construct all configured backends.

    Disabled backends (cfg.*.enabled = False) are skipped — no objects
    created, no threads started.  This mirrors gateway's Init() which
    returns early for each disabled backend.
    """
    backends: list[Backend] = []

    if cfg.prometheus.enabled:
        backends.append(PrometheusBackend(cfg.prometheus, registry))

    if cfg.jaeger.enabled:
        backends.append(JaegerBackend(cfg.jaeger, version, commit))

    if cfg.otel.enabled:
        backends.append(OTelBackend(cfg.otel, version, commit))

    if cfg.elk.enabled:
        backends.append(ELKBackend(cfg.elk, version, commit))

    return backends
