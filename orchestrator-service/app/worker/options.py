"""
app/worker/options.py
──────────────────────
How many syncs a worker runs at once, and when a pool process is replaced.

Each setting is read from the first of these that names it:

  1. the command line     (--concurrency, --max-tasks-per-child)
  2. the environment      (VARTRACK_WORKER_CONCURRENCY, VARTRACK_WORKER_MAX_TASKS_PER_CHILD)
  3. config.cue           (workers: { concurrency, max_tasks_per_child })
  4. the default          (4 and 200)

The machine decides over config.cue: one config.cue is often shared by machines of different
sizes, and how many syncs fit is the machine's to say.

A value that is not a whole number in its range is an error that names where it came from, never
a silent fall-back to the default: an operator who typed "8" and got 4 would not know.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_TASKS_PER_CHILD = 200

MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 64          # the range config.cue's workers.concurrency is checked against (bundle.proto)
MAX_TASKS_PER_CHILD_LIMIT = 2**32 - 1

ENV_CONCURRENCY = "VARTRACK_WORKER_CONCURRENCY"
ENV_MAX_TASKS_PER_CHILD = "VARTRACK_WORKER_MAX_TASKS_PER_CHILD"
# Read for one release: the compose file's variable before it was passed to the worker.
LEGACY_ENV_CONCURRENCY = "WORKER_CONCURRENCY"


class WorkerOptionError(ValueError):
    """A worker setting is not usable. The message names the setting and where it was read."""


@dataclass(frozen=True)
class Setting:
    value: int
    source: str          # where it was read, as the operator would name it

    def __str__(self) -> str:
        return f"{self.value} ({self.source})"


@dataclass(frozen=True)
class WorkerOptions:
    concurrency: Setting
    max_tasks_per_child: Setting
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        return (f"worker: concurrency={self.concurrency}, "
                f"max_tasks_per_child={self.max_tasks_per_child}")


def _whole_number(raw: Any, where: str, low: int, high: int) -> int:
    """*raw* as a whole number in [low, high], or a WorkerOptionError that says where it came from."""
    if isinstance(raw, bool):
        raise WorkerOptionError(f"{where} is {raw!r}, not a whole number from {low} to {high}")
    if isinstance(raw, int):
        number = raw
    else:
        text = str(raw).strip()
        # Digits only: "4.5", "four", "-1" and "0x10" are not what the operator meant to say.
        if not text.isascii() or not text.isdigit():
            raise WorkerOptionError(f"{where} is {text!r}, not a whole number from {low} to {high}")
        number = int(text)
    if not low <= number <= high:
        raise WorkerOptionError(f"{where} is {number}, outside {low} to {high}")
    return number


def _first(candidates: list[tuple[Any, str]], low: int, high: int, default: int) -> Setting:
    """The first candidate that is set, checked; the default when none is."""
    for raw, where in candidates:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        return Setting(_whole_number(raw, where, low, high), where)
    return Setting(default, "default")


def resolve(
    flags: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    bundle_workers: Mapping[str, Any] | None = None,
) -> WorkerOptions:
    """The worker's settings from the command line, the environment and config.cue's workers block.

    *flags* holds "concurrency" and "max_tasks_per_child" (None when not given). *bundle_workers* is the
    bundle's workers block as the loader prints it (either spelling of a key is read).
    """
    flags = flags or {}
    env = env or {}
    cue = bundle_workers or {}
    warnings: list[str] = []

    legacy = env.get(LEGACY_ENV_CONCURRENCY, "").strip()
    if legacy:
        warnings.append(f"{LEGACY_ENV_CONCURRENCY} is read for this release only: "
                        f"set {ENV_CONCURRENCY} (or workers.concurrency in config.cue) instead")

    concurrency = _first(
        [
            (flags.get("concurrency"), "--concurrency"),
            (env.get(ENV_CONCURRENCY), ENV_CONCURRENCY),
            (env.get(LEGACY_ENV_CONCURRENCY), f"{LEGACY_ENV_CONCURRENCY} (deprecated)"),
            (cue.get("concurrency"), "config.cue workers.concurrency"),
        ],
        MIN_CONCURRENCY, MAX_CONCURRENCY, DEFAULT_CONCURRENCY,
    )
    max_tasks = _first(
        [
            (flags.get("max_tasks_per_child"), "--max-tasks-per-child"),
            (env.get(ENV_MAX_TASKS_PER_CHILD), ENV_MAX_TASKS_PER_CHILD),
            (cue.get("max_tasks_per_child", cue.get("maxTasksPerChild")),
             "config.cue workers.max_tasks_per_child"),
        ],
        0, MAX_TASKS_PER_CHILD_LIMIT, DEFAULT_MAX_TASKS_PER_CHILD,
    )
    return WorkerOptions(concurrency, max_tasks, tuple(warnings))
