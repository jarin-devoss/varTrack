from __future__ import annotations

import logging
from threading import Lock

_lock = Lock()
_secrets: list[str] = []
_filter_installed = False


def register_secret(value: str) -> None:
    global _filter_installed
    if not value:
        return
    with _lock:
        if value in _secrets:
            return
        _secrets.append(value)
        if not _filter_installed:
            _install_on_root()
            _filter_installed = True


class SecretMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        with _lock:
            current = list(_secrets)
        if not current:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = msg
        for s in current:
            masked = masked.replace(s, "***")
        if masked != msg:
            record.msg = masked
            record.args = None
        return True


def _install_on_root() -> None:
    f = SecretMaskingFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for handler in root.handlers:
        handler.addFilter(f)
