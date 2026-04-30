"""
app/monitoring/otel.py
───────────────────────
OTel monitoring backend (OTLP traces + metrics).

Ships traces and metrics to any OTLP-compatible backend (Grafana Tempo,
Honeycomb, Lightstep, a local OTel Collector, etc.).
Mirrors gateway's models/monitoring/otel.go OTelBackend.

Requires: opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
"""
from __future__ import annotations

import logging

from app.monitoring.base import Backend
from app.monitoring._utils import _build_otel_resource, _build_otel_sampler
from app.monitoring.config import OTelConfig

logger = logging.getLogger(__name__)


class OTelBackend(Backend):
    """
    Ships traces and metrics to any OTLP-compatible backend.
    Mirrors gateway's models/monitoring/otel.go OTelBackend.
    """

    def __init__(self, cfg: OTelConfig, version: str, commit: str) -> None:
        self._cfg             = cfg
        self._tracer_provider = None
        self._meter_provider  = None

        if not cfg.enabled:
            logger.info("otel backend: disabled")
            return

        try:
            resource = _build_otel_resource(
                cfg.service_name, cfg.service_version or version,
                cfg.environment, cfg.resource_attributes,
            )

            if cfg.enable_traces:
                self._tracer_provider = self._build_tracer_provider(cfg, resource)

            if cfg.enable_metrics:
                self._meter_provider = self._build_meter_provider(cfg, resource)

            # Install propagators (W3C TraceContext + Baggage, or from cfg).
            self._install_propagators(cfg)

            logger.info(
                "otel backend: started endpoint=%s protocol=%s traces=%s metrics=%s",
                cfg.endpoint, cfg.protocol, cfg.enable_traces, cfg.enable_metrics,
            )
        except ImportError:
            logger.warning(
                "otel backend: opentelemetry-sdk not installed — otel disabled. "
                "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
            )
        except Exception:
            logger.exception("otel backend: startup failed")

    def name(self) -> str:
        return "otel"

    def ping(self) -> None:
        errs = []
        for provider in (self._tracer_provider, self._meter_provider):
            if provider is None:
                continue
            try:
                provider.force_flush(timeout_millis=5000)
            except Exception as exc:
                errs.append(str(exc))
        if errs:
            raise RuntimeError(f"otel ping: {'; '.join(errs)}")

    def shutdown(self) -> None:
        for provider in (self._tracer_provider, self._meter_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:
                    logger.warning("otel backend: provider shutdown error", exc_info=True)
        logger.info("otel backend: shut down")

    @staticmethod
    def _build_tracer_provider(cfg: OTelConfig, resource):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        sampler = _build_otel_sampler(cfg.sampler_type, cfg.sampler_ratio)
        tp_kw: dict = {}
        if resource:
            tp_kw["resource"] = resource
        if sampler:
            tp_kw["sampler"] = sampler
        provider = TracerProvider(**tp_kw)

        exporter  = OTelBackend._build_trace_exporter(cfg)
        bsp_kw: dict = {}
        if cfg.max_queue_size > 0:
            bsp_kw["max_queue_size"] = cfg.max_queue_size
        if cfg.batch_timeout_seconds > 0:
            bsp_kw["schedule_delay_millis"] = int(cfg.batch_timeout_seconds * 1000)
        if cfg.max_export_batch > 0:
            bsp_kw["max_export_batch_size"] = cfg.max_export_batch
        provider.add_span_processor(BatchSpanProcessor(exporter, **bsp_kw))

        from opentelemetry import trace
        trace.set_tracer_provider(provider)
        return provider

    @staticmethod
    def _build_meter_provider(cfg: OTelConfig, resource):
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        exporter = OTelBackend._build_metric_exporter(cfg)
        interval = int((cfg.metrics_interval_seconds or 60) * 1000)
        reader   = PeriodicExportingMetricReader(exporter, export_interval_millis=interval)

        mp_kw: dict = {"metric_readers": [reader]}
        if resource:
            mp_kw["resource"] = resource
        provider = MeterProvider(**mp_kw)

        from opentelemetry import metrics
        metrics.set_meter_provider(provider)
        return provider

    @staticmethod
    def _build_trace_exporter(cfg: OTelConfig):
        if cfg.protocol in ("grpc", ""):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            kw: dict = {"endpoint": cfg.endpoint, "headers": cfg.headers or {}}
            if not cfg.use_tls:
                kw["insecure"] = True
            return OTLPSpanExporter(**kw)

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as H
        return H(
            endpoint=f"{'https' if cfg.use_tls else 'http'}://{cfg.endpoint}/v1/traces",
            headers=cfg.headers or {},
        )

    @staticmethod
    def _build_metric_exporter(cfg: OTelConfig):
        if cfg.protocol in ("grpc", ""):
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            kw: dict = {"endpoint": cfg.endpoint, "headers": cfg.headers or {}}
            if not cfg.use_tls:
                kw["insecure"] = True
            return OTLPMetricExporter(**kw)

        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as H
        return H(
            endpoint=f"{'https' if cfg.use_tls else 'http'}://{cfg.endpoint}/v1/metrics",
            headers=cfg.headers or {},
        )

    @staticmethod
    def _install_propagators(cfg: OTelConfig) -> None:
        try:
            from opentelemetry import propagate
            from opentelemetry.propagators.composite import CompositePropagator
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            from opentelemetry.baggage.propagation import W3CBaggagePropagator

            props = []
            for name in (cfg.propagators or ["tracecontext", "baggage"]):
                if name == "tracecontext":
                    props.append(TraceContextTextMapPropagator())
                elif name == "baggage":
                    props.append(W3CBaggagePropagator())
                else:
                    logger.warning("otel: unknown propagator %r — ignored", name)
            if not props:
                props = [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
            propagate.set_global_textmap(CompositePropagator(props))
        except ImportError:
            pass
