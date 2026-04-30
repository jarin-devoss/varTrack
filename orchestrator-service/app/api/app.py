"""
app/api/app.py
───────────────
FastAPI application factory.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Orchestrator API starting up")
    app.state.started_at = time.time()

    # Eagerly init the schema manager so it starts env-configured tenant clones
    from app.schema_registry.manager import get_schema_manager
    app.state.schema_manager = get_schema_manager()

    # Initialise monitoring (idempotent — safe if worker already called it).
    try:
        from app.monitoring import init as monitoring_init
        monitoring_init(
            version=settings.APP_VERSION,
            commit=settings.GIT_COMMIT,
        )
    except Exception:
        logger.warning("monitoring: init failed — metrics will be unavailable", exc_info=True)

    # Celery OTel auto-instrumentation — runs AFTER monitoring_init() so the
    # global TracerProvider is already installed before the instrument attaches.
    # One span per Celery task (task_name, queue, status).
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        CeleryInstrumentor().instrument()
        logger.info("otel: Celery auto-instrumentation enabled")
    except (ImportError, Exception) as exc:
        logger.debug("otel: Celery auto-instrumentation skipped: %s", exc)

    # Start admin server on a SEPARATE port so health probes and Prometheus
    # scrapes never compete with webhook traffic.
    # Runs in a daemon thread — automatically stops when the main process exits.
    try:
        from app.admin.server import get_admin_server
        _admin = get_admin_server()
        _admin.start()
        app.state.admin_server = _admin
    except Exception:
        logger.warning("admin server: failed to start — health/metrics on main port only", exc_info=True)

    yield

    logger.info("Orchestrator API shutting down")
    try:
        _admin = getattr(app.state, "admin_server", None)
        if _admin:
            # Mark unavailable FIRST so readiness probes return 503
            # while the server finishes draining in-flight requests.
            _admin.set_unavailable()
            _admin.stop()
    except Exception:
        pass
    try:
        from app.monitoring import shutdown as monitoring_shutdown
        monitoring_shutdown()
    except Exception:
        pass


def create_app() -> FastAPI:
    # redirect_slashes=False prevents 307 redirects for POST requests with a
    # trailing slash — most webhook senders do not follow redirects for POST.
    # A lightweight ASGI middleware strips the trailing slash from the raw path
    # scope before routing, so POST /v1/webhooks/github/ is handled identically
    # to POST /v1/webhooks/github without a round-trip redirect.
    app = FastAPI(
        title="VarTrack Orchestrator",
        version="0.2.0",
        description="GitOps ETL pipeline — receives webhooks, runs CUE-validated ETL, writes to MongoDB.",
        debug=settings.DEBUG,
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        redirect_slashes=False,
    )

    from starlette.middleware.base import BaseHTTPMiddleware

    class _StripTrailingSlash(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            scope = request.scope
            path = scope.get("path", "")
            if path != "/" and path.endswith("/"):
                scope["path"] = path.rstrip("/")
                # raw_path must stay in sync for routing
                scope["raw_path"] = scope["path"].encode("latin-1")
            return await call_next(request)

    # Middleware stack — Starlette applies in reverse-add order (last = outermost).
    # Desired execution order (outermost → innermost):
    #   SecurityHeaders → CorrelationID → RequestID → CORS → Metrics → StripSlash → router
    from app.monitoring.instrument import MetricsMiddleware
    from app.api.middlewares.security_headers import SecurityHeadersMiddleware
    from app.api.middlewares.correlation import CorrelationIDMiddleware
    from app.api.middlewares.request_id import RequestIDMiddleware
    app.add_middleware(_StripTrailingSlash)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(CorrelationIDMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    # OTel auto-instrumentation — registered here (not in lifespan) so it is
    # part of the middleware stack before the first request arrives.
    # Uses the global TracerProvider which monitoring_init() sets at startup.
    # Safe to call before monitoring_init: if no provider is set yet, the SDK
    # uses a no-op provider and switches automatically once the real one is set.
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.debug("otel: FastAPI auto-instrumentation registered")
    except (ImportError, Exception) as exc:
        logger.debug("otel: FastAPI auto-instrumentation skipped: %s", exc)

    # Routers
    from app.api.routers import webhooks, tasks, schemas, dry_run
    app.include_router(webhooks.router,  prefix="/v1", tags=["webhooks"])
    app.include_router(tasks.router,     prefix="/v1", tags=["tasks"])
    app.include_router(schemas.router,   prefix="/v1", tags=["schemas"])
    app.include_router(dry_run.router,   prefix="/v1", tags=["dry-run"])

    @app.get("/v1/health", tags=["health"])
    @app.get("/v1/health/liveness", tags=["health"])
    async def health_liveness(request: Request) -> dict:
        uptime = time.time() - getattr(request.app.state, "started_at", time.time())
        return {"status": "ok", "uptime_seconds": round(uptime, 1)}

    @app.get("/v1/health/readiness", tags=["health"])
    async def health_readiness(request: Request) -> JSONResponse:
        from app.admin.server import _terminate_requested, _check_broker, _check_redis_backend, _check_grpc_thread
        from app.utils.circuit_breaker import get_breaker, CircuitState

        if _terminate_requested.is_set():
            return JSONResponse(status_code=503,
                                content={"status": "NOT_READY", "detail": "server is terminating"})

        breaker    = get_breaker()
        circuit_ok = breaker.state is not CircuitState.OPEN
        broker_ok  = _check_broker()
        redis_ok   = _check_redis_backend()
        grpc_ok    = _check_grpc_thread()

        if circuit_ok and broker_ok and redis_ok and grpc_ok:
            return JSONResponse(status_code=200,
                                content={"status": "READY", "broker": "ok", "redis": "ok",
                                         "grpc": "ok", "circuit": breaker.state.value})
        return JSONResponse(status_code=503, content={
            "status":  "NOT_READY",
            "broker":  "ok" if broker_ok  else "unreachable",
            "redis":   "ok" if redis_ok   else "unreachable",
            "grpc":    "ok" if grpc_ok    else "dead",
            "circuit": breaker.state.value,
        })

    @app.get("/metrics", tags=["monitoring"], include_in_schema=False)
    async def metrics_endpoint() -> Response:
        """
        Prometheus /metrics endpoint.

        Serves the OrchestratorMetrics registry in OpenMetrics text format.

        Kubernetes pod annotations to enable scraping:
            prometheus.io/scrape: "true"
            prometheus.io/path:   "/metrics"
            prometheus.io/port:   "8000"
        """
        from app.monitoring import get_metrics
        m = get_metrics()
        if m is None:
            return Response(content="# monitoring not initialised\n", media_type="text/plain")
        try:
            content = m.generate_latest_openmetrics()
            media_type = "application/openmetrics-text; version=1.0.0; charset=utf-8"
        except Exception:
            content = m.generate_latest()
            media_type = "text/plain; version=0.0.4; charset=utf-8"
        return Response(content=content, media_type=media_type)

    # Only intercept truly unhandled non-HTTP exceptions; let FastAPI handle
    # RequestValidationError (422) and HTTPException with their correct status codes.
    from fastapi.exceptions import HTTPException as FastAPIHTTPException
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("Validation error: %s", exc.errors())
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(FastAPIHTTPException)
    async def _http_error(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app
