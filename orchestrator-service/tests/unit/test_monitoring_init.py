"""
tests/unit/test_monitoring_init.py

The monitoring package keeps its own logger, whatever its submodules are called.

app/monitoring has a submodule named logger (logger.py). Importing a submodule makes it an attribute of its
package, so `from app.monitoring.logger import configure_logging` replaced the package's `logger` (a logging.Logger)
with that module, and init()'s own `logger.info(...)` then failed with AttributeError: the worker said
"monitoring: init failed in celery worker" and never started its metrics.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path


def test_importing_the_logger_submodule_does_not_replace_the_packages_logger():
    import app.monitoring as monitoring
    import app.monitoring.logger  # noqa: F401 — the import that used to take the name

    assert isinstance(monitoring.log, logging.Logger)
    assert not isinstance(getattr(monitoring, "log"), type(app.monitoring.logger))


def test_the_package_never_calls_a_name_a_submodule_can_take():
    # Every call on `logger` in the package's __init__ would meet the submodule, not a Logger.
    source = Path(__file__).resolve().parents[2] / "app" / "monitoring" / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    submodules = {p.stem for p in source.parent.glob("*.py") if p.stem != "__init__"}
    called = {n.value.id for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    clash = called & submodules
    assert not clash, f"app/monitoring/__init__.py uses {sorted(clash)} as objects, but they are also submodules"
