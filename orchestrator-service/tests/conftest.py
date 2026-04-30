"""
tests/conftest.py

Session-level stub setup for heavy dependencies that are not installed
in the test environment or require live infrastructure.

Executed before any test module is imported, so stubs are present for
every file in the test session.

Dependencies stubbed:
  vartrack_core  — Rust extension (not compiled in test env; pure-Python
                   fallback already in place, but the import guard fires first)
  pymongo        — MongoDB client (no Mongo server in unit tests)
  git / gitpython— GitPython (no repos in unit tests)

Dependencies NOT stubbed (real packages used):
  celery         — installed as a real package; stubs would break submodule
                   imports (celery.schedules, celery.app, etc.)
  fastapi / httpx— installed and required for router tests
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

# ── Register stubs before any app module is imported ─────────────────────────

_STUBS = {
    # Rust extension (pure-Python fallback exists but import guard fires first)
    "vartrack_core": MagicMock(),
    # MongoDB client
    "pymongo": MagicMock(),
    "pymongo.errors": MagicMock(),
    "pymongo.operations": MagicMock(),
    # GitPython (no repos in unit tests)
    "git": MagicMock(),
    "gitpython": MagicMock(),
    # Sink heavy-dependency stubs (libraries not installed in test env)
    "redis": MagicMock(),
    "boto3": MagicMock(),
    "botocore": MagicMock(),
    "kubernetes": MagicMock(),
    "kazoo": MagicMock(),
    "kazoo.client": MagicMock(),
    "kazoo.exceptions": MagicMock(),
    "hvac": MagicMock(),
    "hvac.exceptions": MagicMock(),
    "paramiko": MagicMock(),
}

for _name, _mock in _STUBS.items():
    sys.modules.setdefault(_name, _mock)
