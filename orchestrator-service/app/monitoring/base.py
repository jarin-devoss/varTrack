"""
app/monitoring/base.py
──────────────────────
Backend ABC — the minimal interface every monitoring backend must implement.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Backend(ABC):
    """Minimal interface every monitoring backend must implement."""

    @abstractmethod
    def name(self) -> str: ...

    def ping(self) -> None:
        """Health check — raises on failure.  No-op by default."""

    def shutdown(self) -> None:
        """Flush and release resources.  No-op by default."""
