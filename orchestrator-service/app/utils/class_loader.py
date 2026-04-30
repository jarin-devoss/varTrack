"""
app/utils/class_loader.py
──────────────────────────
Utility for dynamically loading a class from a package sub-module.

Used by BaseSink.load_module(name) to lazily import sink implementations
when they are referenced by name (e.g., from a rule_config "sink_kind" field)
without requiring all sinks to be imported at startup.
"""
from __future__ import annotations

import importlib
import logging
import types

logger = logging.getLogger(__name__)


def load_class_from_package_module(name: str, package: types.ModuleType) -> None:
    """
    Import ``<package>.<name>`` so its classes self-register via IFactory.

    Parameters
    ----------
    name:
        Sub-module name, e.g. "mongo", "redis".
    package:
        The parent package module object.

    Raises
    ------
    ImportError if the sub-module doesn't exist.
    """
    module_name = f"{package.__name__}.{name}"
    try:
        importlib.import_module(module_name)
        logger.debug("class_loader: loaded %s", module_name)
    except ImportError as exc:
        raise ImportError(
            f"class_loader: cannot import {module_name}: {exc}"
        ) from exc
