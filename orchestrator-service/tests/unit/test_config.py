"""
tests/unit/test_config.py

Unit tests for APP_ENV-driven production enforcement in app/config.py.

Covered behaviours:
  - Non-production: MTLS_ENABLED stays False when not set.
  - Production without explicit MTLS_ENABLED: auto-forced to True.
  - Production with MTLS_ENABLED=false: explicit opt-out is respected.
  - Production with MTLS_ENABLED=true: already set, no change.
  - CUE overrides skipped when CONFIG_PATH is absent.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def _settings_env(env: dict[str, str]):
    """
    Context manager that yields a fresh _Settings instance built from env.

    Module is reloaded so class-body os.getenv() calls pick up env.
    pathlib.Path.is_file is patched to False so _load_dotenv() finds no .env
    files on disk, preventing the repo's .env from interfering.
    """
    for mod in list(sys.modules):
        if mod == "app.config" or mod.startswith("app.config."):
            del sys.modules[mod]

    with patch("pathlib.Path.is_file", return_value=False), \
         patch.dict(os.environ, env, clear=True):
        from app.config import _Settings
        yield _Settings()


# ── Non-production defaults ───────────────────────────────────────────────────

def test_default_env_mtls_disabled():
    with _settings_env({"APP_ENV": "development"}) as s:
        assert s.MTLS_ENABLED is False


def test_test_env_mtls_disabled():
    with _settings_env({"APP_ENV": "test"}) as s:
        assert s.MTLS_ENABLED is False


def test_staging_does_not_force_mtls():
    with _settings_env({"APP_ENV": "staging"}) as s:
        assert s.MTLS_ENABLED is False


def test_demo_does_not_force_mtls():
    with _settings_env({"APP_ENV": "demo"}) as s:
        assert s.MTLS_ENABLED is False


# ── Production auto-enforcement ───────────────────────────────────────────────

def test_production_forces_mtls_when_unset():
    """APP_ENV=production without MTLS_ENABLED env var forces MTLS_ENABLED=True."""
    with _settings_env({"APP_ENV": "production"}) as s:
        assert s.MTLS_ENABLED is True


def test_production_respects_explicit_false():
    """APP_ENV=production with MTLS_ENABLED=false keeps it False (escape hatch)."""
    with _settings_env({"APP_ENV": "production", "MTLS_ENABLED": "false"}) as s:
        assert s.MTLS_ENABLED is False


def test_production_with_mtls_already_true():
    """APP_ENV=production with MTLS_ENABLED=true stays True."""
    with _settings_env({"APP_ENV": "production", "MTLS_ENABLED": "true"}) as s:
        assert s.MTLS_ENABLED is True


# ── CUE path absent — overrides silently skipped ──────────────────────────────

def test_missing_config_path_skips_cue_overrides():
    """No CONFIG_PATH → CUE parsing skipped without error; defaults still set."""
    with _settings_env({"APP_ENV": "development"}) as s:
        assert s.CELERY_BROKER_URL  # default is still populated
