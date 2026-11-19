"""
app/worker/warm.py
───────────────────
A pool process has what a sync needs before it takes one.

The first sync a new pool process ran used to take about five times as long as the ones after it
(measured on the e2e stack: about 900 ms against about 100 ms), and so did the next one that used another
kind of store. That happened when a worker started and again each time a process was replaced after
``max_tasks_per_child`` syncs. Two things made it slow, and each is done ahead of time here:

* **The client libraries.** A sink imports its store's library the first time it writes: pymongo, boto3,
  paramiko, hvac, redis and git take about 1.4 s to import together. The worker's main process imports them
  once, when it starts (``worker_init``). Pool processes are forked from it, so every one of them, and every
  replacement, starts with them loaded. An import opens no connection, which is why it is safe before a fork.
* **The tenants' schemas.** Every pool process keeps clones of its own (app/schema_registry/manager.py), and a
  clone of a Git repository must not be shared across a fork, so each new pool process loads them itself, on
  ``worker_process_init``, before it reads its first task. A tenant whose clone fails is said, and its sync
  fails with the reason as it would have. The process waits for the clones for at most SCHEMA_WARM_TIMEOUT
  seconds, then takes its work anyway while a clone still runs.
"""
from __future__ import annotations

import importlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

# What a sync imports the first time it uses a store, in the order of the cost of each (see the module text).
PRELOADED = (
    "app.pipeline.sinks",          # the sink classes: mongo, redis, s3, zookeeper, linux_server, configmap, helm
    "app.utils.vault_client",
    "pymongo",
    "pymongo.errors",
    "boto3",
    "botocore.session",
    "paramiko",
    "hvac",
    "redis",
    "kazoo.client",
    "kazoo.exceptions",
    "git",
)


def preload_libraries(modules: tuple[str, ...] = PRELOADED) -> float:
    """Import what a first sync would import, so a forked pool process finds it loaded. A module that is not
    installed or fails to import is skipped, and said at debug: a sync that needs it fails as it always did.
    Returns the milliseconds it took."""
    started = time.perf_counter()
    loaded = 0
    for name in modules:
        try:
            importlib.import_module(name)
            loaded += 1
        except Exception as exc:
            logger.debug("warm start: %s was not preloaded: %s: %s", name, type(exc).__name__, exc)
    took = (time.perf_counter() - started) * 1000
    logger.info("warm start: %d of %d modules preloaded in %.0f ms", loaded, len(modules), took)
    return took


def _warm_all() -> None:
    """Clone the schemas of every tenant with a schema repository; a tenant that fails is logged."""
    from app import dependencies
    from app.schema_registry.manager import get_schema_manager

    manager = get_schema_manager()
    tenants = manager.list_tenant_ids()
    started = time.perf_counter()
    ready = 0
    for tenant in tenants:
        try:
            manager.get_or_clone(tenant)
            ready += 1
        except Exception as exc:
            # The same failure stops this tenant's sync later, with the same reason: here it is only said.
            logger.warning("warm start: the schemas of tenant %s could not be loaded: %s",
                           tenant, dependencies.scrub(exc))
    if tenants:
        logger.info("warm start: the schemas of %d of %d tenant(s) loaded in %.0f ms",
                    ready, len(tenants), (time.perf_counter() - started) * 1000)


def warm_schemas(timeout: float) -> bool:
    """Load the schemas for at most *timeout* seconds. True when they are loaded, or this was not asked
    for (*timeout* of 0 or less); False when the wait ended first, the clones going on in the background."""
    if timeout <= 0:
        return True
    done = threading.Event()

    def run() -> None:
        try:
            _warm_all()
        except Exception:
            logger.warning("warm start: the schemas could not be loaded", exc_info=True)
        finally:
            done.set()

    # A daemon thread: a process that is stopped while a clone runs does not wait for it.
    threading.Thread(target=run, name="schema-warm", daemon=True).start()
    if done.wait(timeout):
        return True
    logger.warning("warm start: the schemas are still loading after %.0f s; this process takes its work now",
                   timeout)
    return False


def on_worker_init(**_: object) -> None:
    """Celery's worker_init: runs once in the worker's main process, before the pool is forked from it."""
    from app.config import settings

    # The same switch as the schemas': a timeout of 0 turns the warm start off.
    if settings.SCHEMA_WARM_TIMEOUT > 0:
        preload_libraries()


def on_worker_process_init(**_: object) -> None:
    """Celery's worker_process_init: runs in each new pool process, before it reads its first task."""
    from app.config import settings

    warm_schemas(settings.SCHEMA_WARM_TIMEOUT)


def connect() -> None:
    """Warm the worker as it starts, and each pool process as it is forked. Safe to call more than once."""
    from celery.signals import worker_init, worker_process_init

    worker_init.connect(on_worker_init, weak=False, dispatch_uid="vartrack-preload-libraries")
    worker_process_init.connect(on_worker_process_init, weak=False, dispatch_uid="vartrack-warm-schemas")
