"""
app/monitoring/tracer.py
─────────────────────────
Lightweight span tracing.

When opentelemetry-sdk is installed and a TracerProvider is configured,
spans are exported to Jaeger/OTLP (full OTel).  Without it (or during
tests), Span falls back to structlog DEBUG entries with duration —
still observable, zero OTel dependency required.

Convenience wrappers:
  start_webhook_span(task_id, platform, datasource)
  start_etl_span(task_id, stage)
  start_grpc_span(rpc_method)

Usage:
    span = start_webhook_span(task_id="abc", platform="github", datasource="payments")
    try:
        ...
    except Exception as exc:
        span.end(exc)
        raise
    else:
        span.end()
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class Span:
    """
    A single traced operation.

    Wraps an OTel span when the SDK is available; degrades to a structured
    log entry otherwise.
    """

    def __init__(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        _otel_span=None,
    ) -> None:
        self._name      = name
        self._start     = time.perf_counter()
        self._attrs: dict[str, Any] = attrs or {}
        self._otel_span = _otel_span   # None when OTel is not configured

    def set_attr(self, key: str, value: Any) -> "Span":
        """Attach a key-value attribute (used in fallback log + OTel span)."""
        self._attrs[key] = value
        if self._otel_span is not None:
            try:
                self._otel_span.set_attribute(key, str(value))
            except Exception:
                pass
        return self

    def end(self, error: BaseException | None = None) -> float:
        """
        Finalise the span.  Returns elapsed seconds.

        If error is not None the span is recorded as errored.
        """
        elapsed_ms = (time.perf_counter() - self._start) * 1000

        if self._otel_span is not None:
            try:
                from opentelemetry.trace import Status, StatusCode
                if error is not None:
                    self._otel_span.set_status(Status(StatusCode.ERROR, str(error)))
                    self._otel_span.record_exception(error)
                self._otel_span.end()
            except Exception:
                pass

        # Always emit a structlog DEBUG entry (fallback + corroboration).
        extra = {"span": self._name, "duration_ms": round(elapsed_ms, 2), **self._attrs}
        if error is not None:
            logger.debug("trace span finished with error", extra={**extra, "error": str(error)})
        else:
            logger.debug("trace span finished", extra=extra)

        return elapsed_ms / 1000.0


# ── Factory functions ─────────────────────────────────────────────────────────

def _try_otel_span(name: str, attrs: dict[str, Any]):
    """
    Attempt to start an OTel span.  Returns (otel_span | None).

    If the SDK is not installed or no provider is configured, returns None
    and the Span degrades to structured logging only.
    """
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("vartrack.orchestrator")
        span = tracer.start_span(name)
        for k, v in attrs.items():
            span.set_attribute(k, str(v))
        return span
    except Exception:
        return None


def start(name: str, **attrs: Any) -> Span:
    """
    Create a new Span.  Callers must call span.end(err) when done.

        span = start("webhook.process", platform="github")
        try:
            ...
        except Exception as exc:
            span.end(exc)
            raise
        else:
            span.end()
    """
    logger.debug("trace span started", extra={"span": name, **attrs})
    otel = _try_otel_span(name, attrs)
    return Span(name=name, attrs=attrs, _otel_span=otel)


def start_webhook_span(
    task_id: str,
    platform: str,
    datasource: str,
    event_type: str = "",
) -> Span:
    """Convenience wrapper for inbound webhook processing."""
    return start(
        "webhook.process",
        task_id=task_id,
        **{"webhook.platform": platform,
           "webhook.datasource": datasource,
           "webhook.event_type": event_type},
    )


def start_etl_span(task_id: str, stage: str) -> Span:
    """
    Convenience wrapper for ETL pipeline stages (payload / etl / sync).
    """
    return start(
        f"etl.{stage}",
        task_id=task_id,
        **{"etl.stage": stage},
    )


def start_grpc_span(rpc_method: str) -> Span:
    """Convenience wrapper for inbound gRPC calls."""
    return start(
        f"grpc.{rpc_method}",
        **{"rpc.system": "grpc",
           "rpc.method": rpc_method,
           "rpc.service": "vartrack.v1.Orchestrator"},
    )
