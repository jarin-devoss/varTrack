"""
app/monitoring/_utils.py
─────────────────────────
Shared helpers used by multiple monitoring backends.
Not part of the public API — import from specific backend modules instead.
"""
from __future__ import annotations

import ssl


def _build_ssl_context(
    ca_cert: str,
    client_cert: str,
    client_key: str,
    insecure_skip_verify: bool,
) -> ssl.SSLContext | None:
    """Build an ssl.SSLContext from file paths.  Returns None when TLS is off."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if insecure_skip_verify:
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
    if ca_cert:
        ctx.load_verify_locations(ca_cert)
    if client_cert and client_key:
        ctx.load_cert_chain(client_cert, client_key)
    return ctx


def _build_otel_resource(
    service_name: str,
    service_version: str,
    environment: str,
    extra_attrs: dict[str, str],
):
    """
    Build an OTel Resource.  Mirrors gateway's buildOTelResource().
    Returns None when opentelemetry-sdk is not installed.
    """
    try:
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        attrs = {
            SERVICE_NAME:    service_name,
            SERVICE_VERSION: service_version,
        }
        if environment:
            attrs["deployment.environment"] = environment
        attrs.update(extra_attrs)
        return Resource.create(attrs)
    except ImportError:
        return None


def _build_otel_sampler(sampler_type: str, ratio: float):
    """
    Map sampler_type string to an OTel Sampler.
    Mirrors gateway's buildOTelSampler() and buildJaegerSampler().
    """
    try:
        from opentelemetry.sdk.trace.sampling import (
            ALWAYS_ON, ALWAYS_OFF, ParentBased, TraceIdRatioBased,
        )
        mapping = {
            "always_on":                    ALWAYS_ON,
            "always_off":                   ALWAYS_OFF,
            "traceidratio":                 TraceIdRatioBased(ratio),
            "parentbased_always_on":        ParentBased(ALWAYS_ON),
            "parentbased_always_off":       ParentBased(ALWAYS_OFF),
            "parentbased_traceidratio":     ParentBased(TraceIdRatioBased(ratio)),
            # Jaeger compat aliases
            "const":                        ALWAYS_ON if ratio else ALWAYS_OFF,
            "probabilistic":                ParentBased(TraceIdRatioBased(ratio)),
        }
        return mapping.get(sampler_type, ParentBased(ALWAYS_ON))
    except ImportError:
        return None
