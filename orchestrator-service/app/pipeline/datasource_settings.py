"""
app/pipeline/datasource_settings.py
───────────────────────────────────
config.cue as this process reads it, and the credentials in it.

``_bundle()`` is config.cue (re-read when it changes); ``find_datasource`` finds a datasource block by
its name; ``_Secrets`` resolves a credential (a plain string, {value}, {file} or {ref}, the last from the
bundle's secret manager) and raises SecretUnavailable when it cannot be read. A sink is built from its
datasource's block as a typed proto message (app/pipeline/sinks/settings.py), never from flat keys.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from app.dependencies import SecretUnavailable

logger = logging.getLogger(__name__)


# ── config.cue, cached by modification time ─────────────────────────────────

_bundle_lock = threading.Lock()
_bundle_cache: tuple[str, float, dict] | None = None


def _bundle() -> dict:
    """The bundle in CONFIG_PATH, re-read when the file changes."""
    global _bundle_cache
    from app.config import load_bundle, settings

    path = settings.CONFIG_PATH
    if not path:
        return {}       # no config.cue is configured: nothing to read, which is not a failure
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        from app import dependencies

        if _bundle_cache and _bundle_cache[0] == path:
            dependencies.stale("config.cue", f"{path} cannot be read: {exc}")
            return _bundle_cache[2]
        dependencies.down("config.cue", f"{path} cannot be read: {exc}")
        raise dependencies.BundleUnavailable(f"{path} cannot be read: {exc}") from exc
    with _bundle_lock:
        if _bundle_cache and _bundle_cache[0] == path and _bundle_cache[1] == mtime:
            return _bundle_cache[2]
    bundle = load_bundle(path, _bundle_cache[2] if _bundle_cache and _bundle_cache[0] == path else None)
    with _bundle_lock:
        _bundle_cache = (path, mtime, bundle)
    return bundle


def find_datasource(bundle: dict, name: str) -> tuple[Optional[str], Optional[dict]]:
    """The (kind, config) of the datasource named *name*: "{kind}" or "{kind}-{tag}"."""
    for wrapper in bundle.get("datasources") or []:
        if not isinstance(wrapper, dict):
            continue
        for kind, cfg in wrapper.items():
            if not isinstance(cfg, dict):
                continue
            full = f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind
            if full == name:
                return kind, cfg
    return None, None


# ── value conversions ────────────────────────────────────────────────────────

class _Secrets:
    """Resolves SecretRef values. A {ref} is read from the rule's
    secret_manager ("vault-prod"); a rule without one uses the entry tagged
    "default", else the bundle's only one.

    A secret that cannot be read raises SecretUnavailable: it is never the rule's own value, and
    never an anonymous connection, in its place."""

    def __init__(self, bundle: dict, datasource: str, manager: str = "") -> None:
        self._bundle = bundle
        self._datasource = datasource
        self._manager = manager
        self._refs: Any = None

    def __call__(self, value: Any, field: str) -> Optional[str]:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return None
        if "value" in value:
            return str(value["value"])
        if "file" in value:
            from app.utils.secret_file import read_secret_file
            try:
                return read_secret_file(value["file"], value.get("key"))
            except (OSError, ValueError) as exc:
                raise SecretUnavailable(
                    f"{self._datasource}: {field}: cannot read {value['file']}: {exc}", what=self._datasource) from None
        if "ref" in value:
            managers = sorted(
                f"{kind}-{cfg['tag']}" if isinstance(cfg, dict) and cfg.get("tag") else kind
                for wrapper in self._bundle.get("secret_managers") or [] for kind, cfg in wrapper.items()
            )
            name = self._manager
            if not name:
                name = "vault-default" if "vault-default" in managers else (managers[0] if len(managers) == 1 else "")
            problem = ""
            if self._manager and name not in managers:
                problem = f"the rule's secret_manager {name!r} is not in the bundle's secret_managers ({', '.join(managers) or 'none'})"
            elif not name:
                problem = (f"the bundle has {len(managers)} secret managers ({', '.join(managers) or 'none'}); "
                           "name one with secret_manager on the rule, or tag one \"default\"")
            if problem:
                raise SecretUnavailable(f"{self._datasource}: {field} is a {{ref}}, but {problem}", what=self._datasource)
            try:
                if self._refs is None:
                    from app.ui.sessions import _SecretManagerRefs
                    self._refs = _SecretManagerRefs(self._bundle, name)
                return self._refs.get(value, f"datasource {self._datasource}: {field}")
            except Exception as exc:
                raise SecretUnavailable(f"{self._datasource}: {field}: {exc}", what=self._datasource) from None
        return None
