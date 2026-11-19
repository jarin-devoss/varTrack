#!/usr/bin/env python3
"""
How many syncs should a worker run at once? Try the numbers and measure.

A load script for this machine, not one of the regression suites and not run by CI. The e2e stack must be up
(`docker compose up -d` in e2e/, with the regression done). Run it from anywhere:

    python e2e/load/sweep.py                                  # 1, 2, 4, 8, 16 workers; 20, 100, 400 webhooks; 3 runs
    python e2e/load/sweep.py --concurrency 2,4 --counts 100 --runs 1
    python e2e/load/sweep.py rss --tasks 1000                 # does one pool process grow over 1000 syncs?

For each number of pool processes it restarts the worker with VARTRACK_WORKER_CONCURRENCY set, as an operator
would, sends bursts with burst.py (inside the e2e network) and reports:

  * syncs per second and the wall time of the burst (the client's view);
  * the time a task took in the worker, and the time it waited in the queue, from the worker's and the API's
    own logs, so neither includes the client's polling;
  * the peak memory of the worker container (docker stats).

Everything runs on this machine, in Docker Desktop: the numbers say how the settings compare, not what a
production host will do.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
E2E = HERE.parent
COMPOSE = E2E / "docker-compose.yml"
WORKER = "e2e-orchestrator-worker-1"
API = "e2e-orchestrator-api-1"

TASK_START = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}): \w+/[\w-]+\] task_start id=(\S+)")
TASK_DONE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}): \w+/[\w-]+\] stage=sync .*duration_ms=(\d+)")


def run(cmd: list[str], env: dict | None = None, check: bool = True, timeout: float = 1800) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, capture_output=True, text=True, env=full_env, timeout=timeout, encoding="utf-8",
                            errors="replace")
    if check and result.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)}\nexit {result.returncode}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result


def compose(*args: str, env: dict | None = None, check: bool = True, timeout: float = 1800) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(COMPOSE), *args], env=env, check=check, timeout=timeout)


def quantile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def restart_worker(concurrency: int, max_tasks: int | None = None) -> None:
    """Start the worker again with these numbers in its environment, and wait until it is ready."""
    env = {"VARTRACK_WORKER_CONCURRENCY": str(concurrency)}
    if max_tasks is not None:
        env["VARTRACK_WORKER_MAX_TASKS_PER_CHILD"] = str(max_tasks)
    else:
        env["VARTRACK_WORKER_MAX_TASKS_PER_CHILD"] = ""
    compose("up", "-d", "--no-deps", "--force-recreate", "orchestrator-worker", env=env)
    deadline = time.time() + 180
    while time.time() < deadline:
        logs = run(["docker", "logs", WORKER], check=False)
        log = logs.stdout + logs.stderr
        # Every pool process says when it has loaded the schemas (the warm start): all of them up is ready.
        if f"concurrency={concurrency} (" in log and log.count("warm start: the schemas of") >= concurrency:
            time.sleep(2)           # the consumer has connected to the broker by now
            return
        time.sleep(1)
    raise SystemExit("the worker did not become ready in 180 s")


class MemorySampler(threading.Thread):
    """The worker container's memory, sampled while a burst runs."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak_mib = 0.0
        self._halt = threading.Event()      # not "_stop": threading.Thread has a method of that name

    def run(self) -> None:
        while not self._halt.is_set():
            out = run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", WORKER], check=False).stdout
            m = re.match(r"\s*([\d.]+)\s*(KiB|MiB|GiB)", out)
            if m:
                value = float(m[1]) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[m[2]]
                self.peak_mib = max(self.peak_mib, value)

    def stop(self) -> float:
        self._halt.set()
        self.join(timeout=15)
        return self.peak_mib


def parse_worker_log(since: datetime) -> tuple[list[float], dict[str, float]]:
    """Per task: how long it took in the worker (ms), and when it started (id -> epoch seconds)."""
    text = run(["docker", "logs", "--since", since.strftime("%Y-%m-%dT%H:%M:%S"), WORKER], check=False)
    durations: list[float] = []
    starts: dict[str, float] = {}
    for line in (text.stdout + text.stderr).splitlines():
        m = TASK_DONE.match(line)
        if m:
            durations.append(float(m[2]))
            continue
        m = TASK_START.match(line)
        if m:
            when = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc).timestamp()
            starts[m[2]] = when
    return durations, starts


def parse_accepts(since: datetime) -> dict[str, float]:
    text = run(["docker", "logs", "--since", since.strftime("%Y-%m-%dT%H:%M:%S"), API], check=False)
    accepted: dict[str, float] = {}
    for line in (text.stdout + text.stderr).splitlines():
        if "accepted task=" not in line or not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
            task = re.search(r"task=(\S+)", d["event"])[1]
            accepted[task] = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
    return accepted


def burst(count: int) -> dict:
    """One burst, run inside the e2e network. Returns burst.py's summary line."""
    result = run(["docker", "compose", "-f", str(COMPOSE), "run", "--rm", "--no-deps",
                  "-v", f"{HERE}:/load:ro", "--entrypoint", "python", "e2e-regression",
                  "/load/burst.py", "--count", str(count)], check=False, timeout=1500)
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        raise SystemExit(f"burst.py printed no result (exit {result.returncode}):\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
    return json.loads(lines[-1])


def one_run(concurrency: int, count: int) -> dict:
    sampler = MemorySampler()
    # docker logs --since takes whole seconds and the container clocks differ a little from this host's.
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    sampler.start()
    summary = burst(count)
    peak = sampler.stop()
    durations, starts = parse_worker_log(since)
    accepted = parse_accepts(since)
    waits = [(starts[t] - accepted[t]) * 1000 for t in starts if t in accepted and starts[t] - accepted[t] > -1]
    return {
        "concurrency": concurrency, "count": count,
        "ok": summary["ok"], "not_ok": summary["not_ok"],
        "wall_s": summary["wall_s"], "syncs_per_s": summary["syncs_per_s"],
        "client_median_s": summary["accept_to_seen_finished_s"]["median"],
        "client_p90_s": summary["accept_to_seen_finished_s"]["p90"],
        "worker_task_median_ms": round(statistics.median(durations), 0) if durations else None,
        "worker_task_p90_ms": round(quantile(durations, 0.9), 0) if durations else None,
        "queue_wait_median_ms": round(statistics.median(waits), 0) if waits else None,
        "queue_wait_p90_ms": round(quantile(waits, 0.9), 0) if waits else None,
        "peak_mem_mib": round(peak, 0),
    }


def table(rows: list[dict]) -> str:
    """Median over the runs of each (concurrency, count)."""
    groups: dict[tuple[int, int], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["concurrency"], row["count"]), []).append(row)

    def med(rs: list[dict], key: str):
        values = [r[key] for r in rs if r[key] is not None]
        return statistics.median(values) if values else None

    out = ["| processes | webhooks | runs | failed | syncs/s | wall s | worker task median ms | p90 ms | queue wait median ms | p90 ms | peak MiB |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (c, n), rs in sorted(groups.items()):
        cells = [c, n, len(rs), sum(r["not_ok"] for r in rs), med(rs, "syncs_per_s"), med(rs, "wall_s"),
                 med(rs, "worker_task_median_ms"), med(rs, "worker_task_p90_ms"),
                 med(rs, "queue_wait_median_ms"), med(rs, "queue_wait_p90_ms"), med(rs, "peak_mem_mib")]
        out.append("| " + " | ".join("-" if v is None else str(v) for v in cells) + " |")
    return "\n".join(out)


# ── RSS of one pool process over many syncs ───────────────────────────────────

PS_PROGRAM = (
    "import os,json\n"
    "rows=[]\n"
    "for p in os.listdir('/proc'):\n"
    "    if not p.isdigit(): continue\n"
    "    try:\n"
    "        stat=open(f'/proc/{p}/stat').read(); ppid=int(stat.rsplit(')',1)[1].split()[1])\n"
    "        rss=[int(l.split()[1]) for l in open(f'/proc/{p}/status') if l.startswith('VmRSS')][0]\n"
    "        cmd=open(f'/proc/{p}/cmdline').read().replace(chr(0),' ')[:60]\n"
    "    except Exception: continue\n"
    "    rows.append((int(p),ppid,rss,cmd))\n"
    "print(json.dumps(rows))\n"
)


def pool_process_rss_mib() -> list[tuple[int, float]]:
    """(pid, RSS in MiB) of the worker's pool processes: the python processes whose parent is the worker's main process."""
    rows = json.loads(run(["docker", "exec", WORKER, "python", "-c", PS_PROGRAM]).stdout)
    python = [r for r in rows if "python" in r[3]]
    if not python:
        return []
    main_pid = min(r[0] for r in python)
    return [(r[0], round(r[2] / 1024, 1)) for r in python if r[1] == main_pid]


def rss_over_syncs(total: int, step: int) -> list[dict]:
    """One pool process, never replaced, takes `total` syncs; its RSS is read after each burst of `step`."""
    restart_worker(1, max_tasks=0)
    samples = [{"syncs": 0, "rss_mib": pool_process_rss_mib()}]
    done = 0
    while done < total:
        burst(step)
        done += step
        samples.append({"syncs": done, "rss_mib": pool_process_rss_mib()})
        print(f"  {done:5d} syncs  RSS of the pool process(es): {samples[-1]['rss_mib']}", flush=True)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare worker concurrency on the e2e stack")
    parser.add_argument("mode", nargs="?", choices=["sweep", "rss"], default="sweep")
    parser.add_argument("--concurrency", default="1,2,4,8,16", help="pool processes to try")
    parser.add_argument("--counts", default="20,100,400", help="webhooks per burst")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--tasks", type=int, default=1000, help="rss mode: syncs for one pool process")
    parser.add_argument("--step", type=int, default=200, help="rss mode: webhooks per burst")
    parser.add_argument("--out", default="", help="write the raw rows here as JSON")
    args = parser.parse_args()

    if args.mode == "rss":
        samples = rss_over_syncs(args.tasks, args.step)
        if args.out:
            Path(args.out).write_text(json.dumps(samples, indent=1), encoding="utf-8")
        return 0

    rows: list[dict] = []
    for concurrency in [int(c) for c in args.concurrency.split(",") if c]:
        print(f"== {concurrency} pool process(es)", flush=True)
        restart_worker(concurrency)
        for count in [int(n) for n in args.counts.split(",") if n]:
            for i in range(args.runs):
                row = one_run(concurrency, count)
                rows.append(row)
                print(f"  n={count:4d} run {i + 1}: {row['syncs_per_s']} syncs/s, wall {row['wall_s']} s, "
                      f"worker median {row['worker_task_median_ms']} ms, queue {row['queue_wait_median_ms']} ms, "
                      f"peak {row['peak_mem_mib']} MiB, not ok {row['not_ok']}", flush=True)
    print()
    print(table(rows))
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
