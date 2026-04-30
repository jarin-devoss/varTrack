"""
app/monitoring/elk.py
──────────────────────
ELK monitoring backend (Elasticsearch Bulk API + optional Logstash HTTP input).

Mirrors gateway's models/monitoring/elk.go ELKBackend.

Log flow:
  structlog JSON handler
    → multiHandler fan-out
         ├─ original stdout handler  (preserved)
         └─ ELKHandler → buffer → flush worker
              ├─ ES Bulk API       (when logstash is None)
              └─ Logstash HTTP     (when logstash is configured)
"""
from __future__ import annotations

import logging
import threading

from app.monitoring.base import Backend
from app.monitoring._utils import _build_ssl_context
from app.monitoring.config import ELKConfig, ElasticsearchConfig, LogstashConfig

logger = logging.getLogger(__name__)


class ELKBackend(Backend):
    """
    Ships structured log records to Elasticsearch (Bulk API) or via a
    Logstash HTTP input.  Mirrors gateway's models/monitoring/elk.go ELKBackend.
    """

    def __init__(self, cfg: ELKConfig, version: str, commit: str) -> None:
        self._cfg          = cfg
        self._buf: list    = []
        self._buf_lock     = threading.Lock()
        self._flush_event  = threading.Event()
        self._stop_event   = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._prev_handler = None   # restored on shutdown

        if not cfg.enabled:
            logger.info("elk backend: disabled")
            return

        es_cfg = cfg.elasticsearch
        if not es_cfg or not es_cfg.endpoints:
            logger.warning("elk backend: elasticsearch.endpoints required — disabled")
            return

        # Build ES client (always) and optional Logstash writer.
        self._es = self._build_es_client(es_cfg)
        self._ls = self._build_ls_writer(cfg.logstash) if cfg.logstash else None

        # Install structlog multi-processor (preserves stdout handler).
        self._install_log_handler(cfg)

        # Start background flush worker.
        self._flush_thread = threading.Thread(
            target=self._flush_worker,
            daemon=True,
            name="elk-flush",
        )
        self._flush_thread.start()

        logger.info(
            "elk backend: started service=%s index=%s via_logstash=%s",
            cfg.service_name,
            es_cfg.index,
            cfg.logstash is not None,
        )

    def name(self) -> str:
        return "elk"

    def ping(self) -> None:
        if not hasattr(self, "_es") or self._es is None:
            return
        self._es.ping()
        if self._ls is not None:
            self._ls.ping()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=10)
        self._flush()   # final flush
        self._restore_log_handler()
        logger.info("elk backend: shut down")

    # ── enqueue (called by log handler) ───────────────────────────────────────

    def enqueue(self, record: dict) -> None:
        max_docs = self._cfg.elasticsearch.bulk_max_docs or 500
        with self._buf_lock:
            self._buf.append(record)
            should_flush = len(self._buf) >= max_docs
        if should_flush:
            self._flush_event.set()

    # ── flush worker ──────────────────────────────────────────────────────────

    def _flush_worker(self) -> None:
        interval = self._cfg.elasticsearch.flush_interval_seconds or 5.0
        while not self._stop_event.is_set():
            self._flush_event.wait(timeout=interval)
            self._flush_event.clear()
            self._flush()

    def _flush(self) -> None:
        with self._buf_lock:
            if not self._buf:
                return
            batch = self._buf[:]
            self._buf.clear()

        try:
            if self._ls is not None:
                self._ls.write_batch(batch)
            else:
                self._es.bulk_index(batch)
        except Exception:
            logger.warning("elk backend: flush failed", exc_info=True)

    # ── structlog handler installation ────────────────────────────────────────

    def _install_log_handler(self, cfg: ELKConfig) -> None:
        """
        Add a stdlib logging.Handler to the root logger so every log record
        — whether from structlog, FastAPI, uvicorn, Celery or any other
        library that uses logging.getLogger() — is enqueued for ES shipping.

        This mirrors gateway's slog.SetDefault(slog.New(newMultiHandler(...))):
        a second handler is attached alongside the existing stdout handler so
        stdout output is preserved and ELK receives every record.

        Using a stdlib Handler (not a structlog processor) is necessary because
        configure_logging() routes stdlib loggers through ProcessorFormatter's
        foreign_pre_chain, which bypasses the structlog processor chain entirely.
        """
        import datetime as _dt
        import traceback as _tb

        backend   = self
        svc_name  = cfg.service_name
        svc_ver   = cfg.service_version or "dev"
        env_label = cfg.environment
        es_cfg    = cfg.elasticsearch

        class _ELKHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    doc: dict = {
                        "@timestamp":      _dt.datetime.utcnow().strftime(
                                               "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                        "level":           record.levelname.lower(),
                        "message":         record.getMessage(),
                        "logger":          record.name,
                        "service.name":    svc_name,
                        "service.version": svc_ver,
                        "environment":     env_label,
                    }
                    if es_cfg and es_cfg.extra_fields:
                        doc.update(es_cfg.extra_fields)
                    if record.exc_info:
                        doc["exc_info"] = "".join(_tb.format_exception(*record.exc_info))
                    if record.stack_info:
                        doc["stack_info"] = record.stack_info
                    backend.enqueue(doc)
                except Exception:
                    pass  # never crash the logging subsystem

        self._elk_handler = _ELKHandler(level=logging.DEBUG)
        logging.getLogger().addHandler(self._elk_handler)

    def _restore_log_handler(self) -> None:
        """Remove the ELK stdlib handler from the root logger on shutdown."""
        handler = getattr(self, "_elk_handler", None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            self._elk_handler = None

    # ── ES thin client ────────────────────────────────────────────────────────

    @staticmethod
    def _build_es_client(cfg: ElasticsearchConfig) -> "_ESClient":
        return _ESClient(cfg)

    @staticmethod
    def _build_ls_writer(cfg: LogstashConfig) -> "_LogstashWriter":
        return _LogstashWriter(cfg)


class _ESClient:
    """
    Minimal Elasticsearch client using stdlib urllib — mirrors gateway's
    esClient struct.  No third-party elasticsearch-py dependency required.
    """

    def __init__(self, cfg: ElasticsearchConfig) -> None:
        self._cfg = cfg
        self._auth_header = self._build_auth(cfg)
        ssl_ctx = None
        if any([cfg.tls_ca_cert, cfg.tls_client_cert, cfg.insecure_skip_verify]):
            ssl_ctx = _build_ssl_context(
                cfg.tls_ca_cert, cfg.tls_client_cert, cfg.tls_client_key,
                cfg.insecure_skip_verify,
            )
        self._ssl_ctx = ssl_ctx

    @staticmethod
    def _build_auth(cfg: ElasticsearchConfig) -> str:
        if cfg.api_key:
            return f"ApiKey {cfg.api_key}"
        if cfg.bearer_token:
            return f"Bearer {cfg.bearer_token}"
        if cfg.username:
            import base64
            token = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
            return f"Basic {token}"
        return ""

    def ping(self) -> None:
        import urllib.error
        import urllib.request
        if not self._cfg.endpoints:
            raise RuntimeError("no elasticsearch endpoints configured")
        url = self._cfg.endpoints[0].rstrip("/") + "/_cluster/health"
        req = urllib.request.Request(url)
        if self._auth_header:
            req.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise RuntimeError(f"elasticsearch cluster health: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"elasticsearch unreachable: {exc}") from exc

    def bulk_index(self, records: list[dict]) -> None:
        import json as _json
        import urllib.request
        if not records or not self._cfg.endpoints:
            return

        lines = []
        for rec in records:
            lines.append(_json.dumps({"index": {"_index": self._cfg.index}}))
            lines.append(_json.dumps(rec))
        body = "\n".join(lines).encode() + b"\n"

        url = self._cfg.endpoints[0].rstrip("/") + "/_bulk"
        if self._cfg.pipeline:
            url += f"?pipeline={self._cfg.pipeline}"

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-ndjson")
        if self._auth_header:
            req.add_header("Authorization", self._auth_header)

        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"elasticsearch bulk returned HTTP {resp.status}")
        except Exception:
            logger.warning("elk: ES bulk_index failed", exc_info=True)


class _LogstashWriter:
    """
    Logstash HTTP input writer — mirrors gateway's logstashWriter struct.
    Sends NDJSON batches to the Logstash HTTP input plugin.
    """

    def __init__(self, cfg: LogstashConfig) -> None:
        self._cfg = cfg
        self._ssl_ctx = None
        if cfg.use_tls or cfg.tls_ca_cert:
            self._ssl_ctx = _build_ssl_context(
                cfg.tls_ca_cert, cfg.tls_client_cert, cfg.tls_client_key,
                cfg.insecure_skip_verify,
            )

    def _url(self) -> str:
        scheme = "https" if self._cfg.use_tls else "http"
        return f"{scheme}://{self._cfg.endpoint}"

    def ping(self) -> None:
        import urllib.request
        req = urllib.request.Request(self._url(), method="GET")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx,
                                         timeout=self._cfg.timeout_seconds):
                pass
        except Exception as exc:
            raise RuntimeError(f"logstash unreachable: {exc}") from exc

    def write_batch(self, records: list[dict]) -> None:
        import json as _json
        import urllib.request
        if not records:
            return
        body = ("\n".join(_json.dumps(r) for r in records) + "\n").encode()
        req  = urllib.request.Request(self._url(), data=body, method="POST")
        req.add_header("Content-Type", "application/x-ndjson")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx,
                                         timeout=self._cfg.timeout_seconds) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"logstash returned HTTP {resp.status}")
        except Exception:
            logger.warning("elk: Logstash write_batch failed", exc_info=True)
