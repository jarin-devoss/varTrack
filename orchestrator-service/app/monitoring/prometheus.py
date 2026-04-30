"""
app/monitoring/prometheus.py
─────────────────────────────
Prometheus monitoring backend.

Exposes a /metrics scrape endpoint on a dedicated port and optionally
pushes to a Pushgateway.  Mirrors gateway's models/monitoring/prometheus.go.
"""
from __future__ import annotations

import logging
import threading

from app.monitoring.base import Backend
from app.monitoring._utils import _build_ssl_context
from app.monitoring.config import PrometheusConfig

logger = logging.getLogger(__name__)


class PrometheusBackend(Backend):
    """
    Exposes a Prometheus /metrics scrape endpoint on a dedicated port and
    optionally pushes to a Pushgateway.

    Mirrors gateway's models/monitoring/prometheus.go PrometheusBackend:
      - Dedicated HTTP server on cfg.port (separate from the FastAPI port)
      - OTel→Prometheus bridge (when opentelemetry-sdk is installed)
      - Pushgateway loop (background thread, cfg.push_interval_seconds)
      - Basic auth middleware (cfg.basic_auth_user / basic_auth_pass)
      - TLS server (cfg.enable_tls)
    """

    def __init__(self, cfg: PrometheusConfig, registry) -> None:
        self._cfg      = cfg
        self._registry = registry
        self._server   = None
        self._push_stop = threading.Event()

        if not cfg.enabled:
            logger.info("prometheus backend: disabled")
            return

        if cfg.port > 0:
            self._start_server()

        if cfg.push_enabled:
            self._start_push_loop()

        logger.info(
            "prometheus backend: started port=%d path=%s push=%s",
            cfg.port, cfg.metrics_path, cfg.push_enabled,
        )

    def name(self) -> str:
        return "prometheus"

    def ping(self) -> None:
        """GET /metrics on the local scrape server."""
        if self._server is None:
            return
        import http.client
        host = f"localhost:{self._cfg.port}"
        path = self._cfg.metrics_path or "/metrics"
        try:
            conn = http.client.HTTPConnection(host, timeout=3)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                raise RuntimeError(f"prometheus ping: HTTP {resp.status}")
        finally:
            conn.close()

    def shutdown(self) -> None:
        self._push_stop.set()
        if self._server is not None:
            self._server.shutdown()
            logger.info("prometheus backend: shut down")

    def _start_server(self) -> None:
        """
        Start a minimal HTTP server serving /metrics and /healthz.
        Mirrors gateway's startServer() with basic auth + TLS support.
        """
        import http.server
        from prometheus_client import CONTENT_TYPE_LATEST

        cfg      = self._cfg
        registry = self._registry

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # silence access log; metrics server is high-frequency

            def _check_auth(self) -> bool:
                if not cfg.basic_auth_user:
                    return True
                import base64
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    return False
                decoded = base64.b64decode(auth[6:]).decode()
                user, _, pwd = decoded.partition(":")
                return user == cfg.basic_auth_user and pwd == cfg.basic_auth_pass

            def do_GET(self):
                if not self._check_auth():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="metrics"')
                    self.end_headers()
                    return

                path = cfg.metrics_path or "/metrics"
                if self.path == "/healthz":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                elif self.path == path:
                    from prometheus_client import generate_latest
                    output = generate_latest(registry)
                    self.send_response(200)
                    self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                    self.end_headers()
                    self.wfile.write(output)
                else:
                    self.send_response(404)
                    self.end_headers()

        server = http.server.HTTPServer(("", cfg.port), _Handler)

        if cfg.enable_tls and (cfg.tls_cert or cfg.tls_ca):
            ctx = _build_ssl_context(
                cfg.tls_ca, cfg.tls_cert, cfg.tls_key, cfg.insecure_skip_verify
            )
            server.socket = ctx.wrap_socket(server.socket, server_side=True)

        t = threading.Thread(target=server.serve_forever, daemon=True, name="prom-scrape-server")
        t.start()
        self._server = server
        logger.info("prometheus scrape server: listening on :%d%s", cfg.port, cfg.metrics_path)

    def _start_push_loop(self) -> None:
        """
        Background thread that pushes metrics to a Pushgateway.
        Mirrors gateway's startPushLoop() with grouping labels.
        """
        cfg      = self._cfg
        registry = self._registry
        stop     = self._push_stop

        def _loop():
            from prometheus_client import push_to_gateway
            interval = cfg.push_interval_seconds or 15.0
            while not stop.wait(interval):
                try:
                    groupings = {k: v for k, v in cfg.external_labels.items()}
                    push_to_gateway(
                        gateway=cfg.push_url,
                        job=cfg.push_job_name or "orchestrator",
                        registry=registry,
                        grouping_key=groupings or None,
                    )
                except Exception:
                    logger.warning(
                        "prometheus push failed url=%s", cfg.push_url, exc_info=True
                    )

        t = threading.Thread(target=_loop, daemon=True, name="prom-push-loop")
        t.start()
        logger.info(
            "prometheus push loop: url=%s interval=%ss",
            cfg.push_url, cfg.push_interval_seconds,
        )
