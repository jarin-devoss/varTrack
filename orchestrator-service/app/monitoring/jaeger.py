"""
app/monitoring/jaeger.py
─────────────────────────
Jaeger monitoring backend (OTLP/gRPC or OTLP/HTTP).

Ships spans to a Jaeger collector.  Mirrors gateway's
models/monitoring/jaeger.go JaegerBackend.

Requires: opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
          (or opentelemetry-exporter-otlp-proto-http for thrift_http)
"""
from __future__ import annotations

import logging

from app.monitoring.base import Backend
from app.monitoring._utils import _build_ssl_context, _build_otel_resource, _build_otel_sampler
from app.monitoring.config import JaegerConfig

logger = logging.getLogger(__name__)


class JaegerBackend(Backend):
    """
    Ships spans to a Jaeger collector via OTLP/gRPC or OTLP/HTTP.
    Mirrors gateway's models/monitoring/jaeger.go JaegerBackend.

    When opentelemetry-sdk is not installed, this backend is a no-op
    (same degradation as gateway's Span falling back to slog entries).
    """

    def __init__(self, cfg: JaegerConfig, version: str, commit: str) -> None:
        self._cfg      = cfg
        self._provider = None

        if not cfg.enabled:
            logger.info("jaeger backend: disabled")
            return

        try:
            self._provider = self._build_provider(cfg, version)
            logger.info(
                "jaeger backend: started endpoint=%s protocol=%s service=%s sampler=%s",
                cfg.endpoint, cfg.protocol, cfg.service_name, cfg.sampler_type,
            )
        except ImportError:
            logger.warning(
                "jaeger backend: opentelemetry-sdk not installed — traces disabled. "
                "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
            )
        except Exception:
            logger.exception("jaeger backend: startup failed")

    def name(self) -> str:
        return "jaeger"

    def ping(self) -> None:
        if self._provider is None:
            return
        try:
            self._provider.force_flush(timeout_millis=5000)
        except Exception as exc:
            raise RuntimeError(f"jaeger ping: {exc}") from exc

    def shutdown(self) -> None:
        if self._provider is not None:
            try:
                self._provider.shutdown()
            except Exception:
                logger.warning("jaeger backend: shutdown error", exc_info=True)
        logger.info("jaeger backend: shut down")

    @staticmethod
    def _build_provider(cfg: JaegerConfig, version: str):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = _build_otel_resource(
            cfg.service_name, cfg.service_version or version,
            cfg.environment, cfg.resource_attributes,
        )
        sampler = _build_otel_sampler(cfg.sampler_type, cfg.sampler_param)

        tp_kwargs: dict = {}
        if resource:
            tp_kwargs["resource"] = resource
        if sampler:
            tp_kwargs["sampler"] = sampler

        provider = TracerProvider(**tp_kwargs)

        exporter = JaegerBackend._build_exporter(cfg)
        bsp_kwargs: dict = {}
        if cfg.max_queue_size > 0:
            bsp_kwargs["max_queue_size"] = cfg.max_queue_size
        if cfg.flush_interval_seconds > 0:
            bsp_kwargs["schedule_delay_millis"] = int(cfg.flush_interval_seconds * 1000)

        provider.add_span_processor(BatchSpanProcessor(exporter, **bsp_kwargs))

        # Install as global so otelhttp / otelgrpc auto-instrumentation works.
        from opentelemetry import trace
        trace.set_tracer_provider(provider)

        # W3C TraceContext + Baggage propagation (same as gateway's Propagator()).
        from opentelemetry import propagate
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        propagate.set_global_textmap(
            CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
        )

        return provider

    @staticmethod
    def _build_exporter(cfg: JaegerConfig):
        if cfg.protocol in ("grpc", ""):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            kwargs: dict = {"endpoint": cfg.endpoint}
            if cfg.use_tls:
                import grpc
                _build_ssl_context(cfg.tls_ca_cert, cfg.tls_client_cert, cfg.tls_client_key, cfg.insecure_skip_verify)
                kwargs["credentials"] = grpc.ssl_channel_credentials()
            else:
                kwargs["insecure"] = True
            if cfg.auth_token:
                kwargs["headers"] = {"Authorization": f"Bearer {cfg.auth_token}"}
            return OTLPSpanExporter(**kwargs)

        # thrift_http → OTLP/HTTP
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HTTPExporter
        kwargs = {"endpoint": f"{'https' if cfg.use_tls else 'http'}://{cfg.endpoint}/v1/traces"}
        if cfg.auth_token:
            kwargs["headers"] = {"Authorization": f"Bearer {cfg.auth_token}"}
        return HTTPExporter(**kwargs)
