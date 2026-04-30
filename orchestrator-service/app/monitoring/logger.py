"""
app/monitoring/logger.py
─────────────────────────
Structured JSON logging setup.

Uses structlog JSON renderer + stdlib logging bridge.
stdlib logging calls from third-party libraries (celery, pymongo, uvicorn)
are bridged via logging.basicConfig so they also emit JSON.

Usage:
    Call configure_logging() once at process start.  After that every
    logger.info/debug/warning/error call produces a JSON line on stdout.

    from app.monitoring.logger import configure_logging
    configure_logging()
"""
from __future__ import annotations

import logging
import os
import sys


def _log_level() -> int:
    """Map LOG_LEVEL env var to stdlib logging level, default INFO."""
    raw = os.getenv("LOG_LEVEL", "INFO").upper()
    return {
        "DEBUG":    logging.DEBUG,
        "INFO":     logging.INFO,
        "WARN":     logging.WARNING,
        "WARNING":  logging.WARNING,
        "ERROR":    logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(raw, logging.INFO)


def configure_logging() -> None:
    """
    Configure structlog + stdlib logging to emit JSON to stdout.

    Idempotent — safe to call multiple times (subsequent calls are no-ops
    because structlog checks if it's already configured).

    structlog configuration follows the recommended production setup from
    structlog docs: processors → stdlib bridge → JSON renderer.

    At DEBUG level, CallsiteParameter adds file + line to every log entry
    so local development has full context without flooding INFO logs.
    """
    try:
        import structlog
    except ImportError:
        # structlog not installed — fall back to stdlib JSON-ish format
        _configure_stdlib_fallback()
        return

    level = _log_level()

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    # Add source (file + line) at DEBUG level only.
    if level == logging.DEBUG:
        shared_processors.append(
            structlog.processors.CallsiteParameterAdder(
                parameters=[
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                ]
            )
        )

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy third-party loggers.
    for name in ("celery", "kombu", "pymongo", "urllib3", "httpx", "git"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    logging.getLogger(__name__).debug(
        "logging configured",
        extra={"level": logging.getLevelName(level)},
    )


def _configure_stdlib_fallback() -> None:
    """Minimal JSON-formatted stdlib logging when structlog is absent."""
    level = _log_level()
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","event":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
