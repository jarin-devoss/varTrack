"""
app/monitoring/instrument.py
─────────────────────────────
FastAPI/ASGI middleware that records HTTP metrics into OrchestratorMetrics.

Instruments:
  - orch_http_requests_total        {method, path, status_code}
  - orch_http_request_duration_seconds {method, path}
  - orch_http_request_body_bytes    {path}
  - orch_http_active_requests       {method, path}

Path normalisation: route template is used (e.g. /v1/webhooks/{datasource})
instead of the raw URL so cardinality stays bounded.

Usage (wired in create_app):
    app.add_middleware(MetricsMiddleware)
"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match


class MetricsMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that feeds HTTP layer metrics into OrchestratorMetrics."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Lazy import — metrics may not be initialised in test environments that
        # don't call init().  If not ready, pass through silently.
        from app.monitoring import get_metrics
        metrics = get_metrics()
        if metrics is None:
            return await call_next(request)

        method = request.method
        path   = _resolve_route(request)

        # Track payload size from Content-Length (available without body read).
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                metrics.observe_http_body_bytes(path, float(content_length))
            except ValueError:
                pass

        metrics.inc_active_requests(method, path)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.inc_http_request(method, path, "500")
            metrics.observe_http_duration(method, path, time.perf_counter() - start)
            metrics.dec_active_requests(method, path)
            raise

        elapsed = time.perf_counter() - start
        metrics.inc_http_request(method, path, str(response.status_code))
        metrics.observe_http_duration(method, path, elapsed)
        metrics.dec_active_requests(method, path)
        return response


def _resolve_route(request: Request) -> str:
    """
    Return the matched route template, or the raw path if no route matches.

    Uses the route template (e.g. /v1/webhooks/{datasource}) instead of
    the raw URL so the label set doesn't explode with one entry per datasource name.
    """
    # /metrics itself should never be instrumented (avoid recursion).
    raw = request.url.path
    if raw in ("/metrics", "/v1/metrics"):
        return raw

    app = request.app
    # FastAPI stores the Router on app.router; iterate routes to find a match.
    if hasattr(app, "router"):
        for route in app.router.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL and hasattr(route, "path"):
                return route.path

    # Fallback: strip query string, keep raw path
    return raw.split("?")[0]
