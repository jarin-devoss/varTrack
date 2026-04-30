"""
main.py  –  VarTrack Orchestrator entry point
──────────────────────────────────────────────
Development:
    python main.py

Production (uvicorn):
    uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

Production (gunicorn + uvicorn workers):
    gunicorn main:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000

Celery worker:
    celery -A app.worker.celery worker --queues=webhooks,sync -l info

Celery beat (periodic self-heal):
    celery -A app.worker.celery beat -l info

Flower (monitoring):
    celery -A app.worker.celery flower --port=5555

gRPC server note
────────────────
The Go gateway dials the gRPC port at startup and blocks until connectivity.Ready.
We start the gRPC server in a daemon thread so it is alive before uvicorn starts
accepting HTTP.

daemon=True guarantees the thread is reaped automatically when the master process
exits (uvicorn shutdown, SIGTERM, or crash) — no extra cleanup logic required.
"""
import logging
import os
import threading

import uvicorn

from app.api.app import create_app
from app.config import settings

logger = logging.getLogger(__name__)

# ASGI app used by uvicorn / gunicorn
app = create_app()


def _grpc_rule_resolver(platform: str, datasource: str):
    """Lazy rule resolver for the gRPC server.

    Searches all registered tenants for a matching rule.  The gRPC path does not
    carry a tenant header, so we scan every tenant and return the first match.
    """
    try:
        from app.schema_registry.manager import get_schema_manager
        mgr = get_schema_manager()
        if mgr is None:
            return None
        # Scan every loaded tenant (entries keyed as "tenant_id:slug")
        with mgr._lock:
            for k, entry in mgr._entries.items():
                for rule in entry.rules:
                    if rule.get("platform") == platform and rule.get("datasource") == datasource:
                        return rule
        return None
    except Exception:
        return None


def _start_grpc_server() -> None:
    """Launch the gRPC server in a daemon thread.

    Called once at import time so the gRPC port is open before uvicorn
    starts — this prevents a race where the Go gateway fails its readiness
    connection-wait loop.
    """
    try:
        from app.grpc_server.server import run_server

        grpc_port = int(os.environ.get("GRPC_PORT", "50051"))
        thread = threading.Thread(
            target=run_server,
            kwargs={"port": grpc_port, "rule_registry": _grpc_rule_resolver},
            daemon=True,           # Killed automatically when master exits
            name="grpc-server",
        )
        thread.start()
        logger.info("gRPC server thread started on port %d", grpc_port)
    except Exception:
        # Log but do not crash the HTTP process — the Go gateway circuit
        # breaker will surface gRPC errors if the thread dies.
        logger.exception("Failed to start gRPC server thread")


# Start immediately at import time so gunicorn/uvicorn workers all have it.
_start_grpc_server()


if __name__ == "__main__":
    from app.tls import build_ssl_context
    ssl_ctx = build_ssl_context()

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="debug" if settings.DEBUG else "info",
        ssl=ssl_ctx,
    )
