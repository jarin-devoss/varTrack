"""
app/worker/__main__.py
───────────────────────
Start a worker:

    python -m app.worker

It reads how many syncs to run at once, and when to replace a pool process, from the command
line, the environment and config.cue (app/worker/options.py says in what order), says in one log
line where each value came from, and starts Celery with them. The queues are the ones a worker has
always served: webhooks, sync and schema.

    python -m app.worker --concurrency 8
    VARTRACK_WORKER_CONCURRENCY=8 python -m app.worker

Anything else on the command line goes to Celery as it is (--hostname, -E, ...).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence

from app.worker import options

DEFAULT_QUEUES = "webhooks,sync,schema"

logger = logging.getLogger("app.worker")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Start an orchestrator worker. Other arguments go to `celery worker` unchanged.",
        allow_abbrev=False,
    )
    # Taken as text: options.resolve says what is wrong with a value and where it came from.
    parser.add_argument("-c", "--concurrency", default=None,
                        help=f"syncs this worker runs at once, {options.MIN_CONCURRENCY} to {options.MAX_CONCURRENCY} "
                             f"(else {options.ENV_CONCURRENCY}, config.cue workers.concurrency, "
                             f"then {options.DEFAULT_CONCURRENCY})")
    parser.add_argument("--max-tasks-per-child", default=None, dest="max_tasks_per_child",
                        help="replace a pool process after this many syncs, 0 for never "
                             f"(else {options.ENV_MAX_TASKS_PER_CHILD}, config.cue workers.max_tasks_per_child, "
                             f"then {options.DEFAULT_MAX_TASKS_PER_CHILD})")
    parser.add_argument("-Q", "--queues", default=DEFAULT_QUEUES, help="queues to serve")
    parser.add_argument("-l", "--loglevel", default="info", help="Celery's log level")
    return parser


def celery_arguments(opts: options.WorkerOptions, queues: str, loglevel: str, extra: Sequence[str]) -> list[str]:
    """The arguments `celery worker` is started with."""
    argv = ["worker", f"--loglevel={loglevel}", "-Q", queues, f"--concurrency={opts.concurrency.value}"]
    if opts.max_tasks_per_child.value:
        argv.append(f"--max-tasks-per-child={opts.max_tasks_per_child.value}")
    return [*argv, *extra]


def main(argv: Sequence[str] | None = None) -> int:
    args, extra = _parser().parse_known_args(argv)

    # Before Celery sets up its own logging, which then takes over the same format.
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s: %(levelname)s/MainProcess] %(message)s")

    # app.config reads .env and config.cue: what the environment names is taken after it.
    from app.config import settings

    try:
        opts = options.resolve(
            flags={"concurrency": args.concurrency, "max_tasks_per_child": args.max_tasks_per_child},
            env=os.environ,
            bundle_workers=settings.WORKERS,
        )
    except options.WorkerOptionError as exc:
        print(f"worker: not started: {exc}", file=sys.stderr, flush=True)
        return 2

    for warning in opts.warnings:
        logger.warning("%s", warning)
    logger.info("%s", opts.describe())

    from app.worker.celery import celery

    if not opts.max_tasks_per_child.value:
        # 0 is "never replace a process": the app's own default (a replacement after
        # options.DEFAULT_MAX_TASKS_PER_CHILD syncs) must not apply.
        celery.conf.worker_max_tasks_per_child = None

    return celery.worker_main(celery_arguments(opts, args.queues, args.loglevel, extra)) or 0


if __name__ == "__main__":
    sys.exit(main())
