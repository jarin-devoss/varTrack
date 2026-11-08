"""
app/admin/server.py
────────────────────
Admin HTTP server on a SEPARATE port from the public webhook API.

Endpoints:
  GET /healthz                → quick liveness probe
  GET /health/liveness        → liveness check
  GET /health/readiness       → readiness check (Celery broker reachable)
  GET /metrics                → Prometheus scrape
  GET /debug/pprof            → Python traceback / memory snapshot (DEBUG mode only)
  GET /circuit-breaker        → circuit breaker state (JSON)

Design:
  • Uses stdlib http.server — zero extra dependencies.
  • Runs in a daemon thread started from the FastAPI lifespan hook so it
    shares the same process (and thus the same metrics registry and
    circuit-breaker singleton) as the main API.
  • Prometheus /metrics is exposed ONLY on the admin port so webhook
    traffic and scrape traffic are isolated.
  • When DEBUG=true the /debug/pprof endpoint returns a Python traceback
    dump (all threads).

Access:
  • Binds to ADMIN_HOST, which is loopback (127.0.0.1) unless set otherwise.
  • Health probes (/healthz, /health/*) are always open.
  • With INTERNAL_AUTH_SECRET set, everything else (/metrics, /circuit-breaker,
    /debug/pprof) needs an "admin" grant: Authorization: Bearer <grant>, minted
    with python -m app.api.tokens mint --audience admin.

ADMIN_PORT env var (default 9090) controls the port.
ADMIN_ENABLE_PPROF env var (default: equals DEBUG) controls /debug/pprof.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)

# Open to every caller: the probes an orchestrator or load balancer makes.
_OPEN_PATHS = frozenset({"/healthz", "/health/liveness", "/health/readiness"})
_LOOPBACK   = frozenset({"127.0.0.1", "::1", "localhost"})

# ── Startup timestamp + graceful-shutdown flag ───────────────────────────────
_started_at: float = time.time()
_terminate_requested: threading.Event = threading.Event()


# ── Request handler ───────────────────────────────────────────────────────────

class _AdminHandler(BaseHTTPRequestHandler):
    """Minimal HTTP/1.1 handler for admin endpoints. No external dependencies."""

    # Suppress per-request access logs (too noisy for scrape traffic);
    # errors are still logged via log_error.
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def log_error(self, fmt: str, *args: Any) -> None:
        logger.warning("admin: " + fmt, *args)

    # ── Dispatch ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path not in _OPEN_PATHS and not self._authorized():
            return

        if path in ("/healthz", "/health/liveness"):
            self._liveness()
        elif path == "/health/readiness":
            self._readiness()
        elif path == "/metrics":
            self._metrics()
        elif path == "/circuit-breaker":
            self._circuit_breaker()
        elif path.startswith("/debug/pprof"):
            self._pprof()
        else:
            self._not_found()

    # ── Access ────────────────────────────────────────────────────────────────

    def _authorized(self) -> bool:
        """True when the caller may see the non-probe endpoints. While internal
        authentication is off (development) everyone may; once it is on, only a
        valid admin grant. Writes the 401 itself when it returns False."""
        from app.api import tokens
        if not tokens.enforced():
            return True
        try:
            tokens.verify(tokens.bearer(self.headers.get("Authorization")), tokens.AUDIENCE_ADMIN)
            return True
        except tokens.GrantError:
            self._json(401, {"detail": "an admin grant is required: Authorization: Bearer <grant>"})
            return False

    # ── Endpoints ─────────────────────────────────────────────────────────────

    def _liveness(self) -> None:
        """Always 200 while the process is alive."""
        self._json(200, {
            "status":        "ok",
            "uptime_seconds": round(time.time() - _started_at, 1),
        })

    def _readiness(self) -> None:
        """
        200 = READY   (not terminating + broker + Redis + gRPC thread + circuit)
        503 = NOT_READY
        """
        if _terminate_requested.is_set():
            self._json(503, {"status": "NOT_READY", "detail": "server is terminating"})
            return

        from app.utils.circuit_breaker import get_breaker, CircuitState
        breaker    = get_breaker()
        circuit_ok = breaker.state is not CircuitState.OPEN
        broker_ok  = _check_broker()
        redis_ok   = _check_redis_backend()
        grpc_ok    = _check_grpc_thread()

        from app import dependencies

        extra = dependencies.readiness()
        if circuit_ok and broker_ok and redis_ok and grpc_ok and "unavailable" not in extra:
            self._json(200, {"status": "READY", "broker": "ok", "redis": "ok",
                             "grpc": "ok", "circuit": breaker.state.value, **extra})
        else:
            self._json(503, {
                "status":  "NOT_READY",
                "broker":  "ok" if broker_ok  else "unreachable",
                "redis":   "ok" if redis_ok   else "unreachable",
                "grpc":    "ok" if grpc_ok    else "dead",
                "circuit": breaker.state.value,
                **extra,
            })

    def _metrics(self) -> None:
        """Prometheus /metrics scrape endpoint (OpenMetrics text format)."""
        from app.monitoring import get_metrics
        m = get_metrics()
        if m is None:
            self._text(200, "# monitoring not initialised\n")
            return
        try:
            content   = m.generate_latest_openmetrics()
            mime_type = "application/openmetrics-text; version=1.0.0; charset=utf-8"
        except Exception:
            content   = m.generate_latest()
            mime_type = "text/plain; version=0.0.4; charset=utf-8"
        self._respond(200, content, mime_type)

    def _circuit_breaker(self) -> None:
        """Return circuit-breaker state as JSON."""
        from app.utils.circuit_breaker import get_breaker
        self._json(200, get_breaker().state_dict())

    def _pprof(self) -> None:
        """
        Dump all thread stacks.
        Only available when ADMIN_ENABLE_PPROF=true (or DEBUG=true).
        """
        enable_pprof = os.getenv("ADMIN_ENABLE_PPROF", os.getenv("DEBUG", "")).lower() in (
            "1", "true", "yes"
        )
        if not enable_pprof:
            self._json(403, {"error": "pprof not enabled — set ADMIN_ENABLE_PPROF=true"})
            return
        import sys
        lines: list[str] = []
        for thread_id, frame in sys._current_frames().items():
            lines.append(f"\n=== Thread {thread_id} ===\n")
            lines.append("".join(traceback.format_stack(frame)))
        self._text(200, "".join(lines))

    def _not_found(self) -> None:
        self._json(404, {"error": "not found", "path": self.path})

    # ── Response helpers ──────────────────────────────────────────────────────

    def _json(self, status: int, body: Any) -> None:
        self._respond(status, json.dumps(body), "application/json")

    def _text(self, status: int, body: str) -> None:
        self._respond(status, body, "text/plain")

    def _respond(self, status: int, body: str | bytes, content_type: str) -> None:
        encoded = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ── Broker probe ──────────────────────────────────────────────────────────────

def _check_broker() -> bool:
    """
    Fast broker connectivity probe — does NOT enqueue tasks.
    Uses Celery's connection_for_read() pool (non-blocking, 2s timeout).
    """
    try:
        from app.worker.celery import celery
        conn = celery.connection_for_read()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.release()
        return True
    except Exception:
        return False


# ── Redis backend probe ───────────────────────────────────────────────────────

def _check_redis_backend() -> bool:
    """
    PING the Celery result backend (Redis).
    Uses a short socket timeout so health probes never hang.
    """
    try:
        from app.worker.celery import celery
        backend_url: str = celery.conf.result_backend or ""
        if not backend_url.startswith("redis"):
            # Non-Redis backend (e.g. RPC) — skip, treat as ok.
            return True
        import redis as _redis
        client = _redis.from_url(backend_url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return True
    except Exception:
        return False


# ── gRPC server-thread probe ──────────────────────────────────────────────────

def _check_grpc_thread() -> bool:
    """
    Verify the gRPC server thread is alive.
    The thread is registered under the name 'grpc-server' in app/grpc/server.py.
    """
    for t in threading.enumerate():
        if t.name == "grpc-server":
            return t.is_alive()
    # Thread not found — either not started yet (startup phase) or dead.
    # Return True during startup window (process age < 30 s) to avoid
    # false 503 before the gRPC thread has been registered.
    return (time.time() - _started_at) < 30.0


# ── Admin server lifecycle ────────────────────────────────────────────────────

class AdminServer:
    """Thin wrapper around HTTPServer.  Runs in a daemon thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9090) -> None:
        self._host = host
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the admin server in a background daemon thread."""
        global _started_at
        _started_at = time.time()

        if self._host not in _LOOPBACK:
            from app.api import tokens
            if not tokens.enforced():
                logger.warning(
                    "admin server listens on %s without authentication: /metrics, /circuit-breaker and "
                    "/debug/pprof are open to anyone who can reach it. Set INTERNAL_AUTH_SECRET, or keep "
                    "ADMIN_HOST on loopback.", self._host,
                )
        self._server = HTTPServer((self._host, self._port), _AdminHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="admin-server",
            daemon=True,   # exits with the main process
        )
        self._thread.start()
        logger.info("admin server started addr=%s:%d", self._host, self._port)

    def set_unavailable(self) -> None:
        """Signal that the process is shutting down; readiness probe returns 503."""
        _terminate_requested.set()

    def stop(self) -> None:
        """Gracefully stop the admin server."""
        if self._server:
            self._server.shutdown()
            logger.info("admin server stopped")


# ── Module-level singleton ────────────────────────────────────────────────────

_admin_server: AdminServer | None = None
_admin_lock   = threading.Lock()


def get_admin_server() -> AdminServer:
    """Return the singleton AdminServer, creating it on first call."""
    global _admin_server
    if _admin_server is None:
        with _admin_lock:
            if _admin_server is None:
                from app.config import settings
                _admin_server = AdminServer(
                    host=getattr(settings, "ADMIN_HOST", "127.0.0.1"),
                    port=getattr(settings, "ADMIN_PORT", 9090),
                )
    return _admin_server
