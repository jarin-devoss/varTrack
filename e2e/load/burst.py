#!/usr/bin/env python3
"""
A burst of webhooks sent at once, to see how many syncs a worker finishes in a second.

This is a load script, not one of the regression suites: it asserts nothing and is not run by CI. It runs
inside the e2e network, with the runner's image (e2e/load/sweep.py starts it), and reuses the runner's own
helpers, so it signs its calls and builds its rules exactly as the suites do:

    docker compose run --rm --no-deps -v ./load:/load:ro --entrypoint python e2e-regression \\
        /load/burst.py --count 100 --out /results/burst/run.json

It pushes one bundle with a Mongo, a Redis and an S3 rule (each writing under a destination of its own),
warms the schemas, then sends --count push webhooks at once, round-robin over the rules, and waits for
every task. It reports, from the client's side: how many finished, the wall time from the first send to
the last finish, syncs per second, and the time from "webhook accepted" to "seen finished" (median, p90,
max). The time a task waited in the queue and the time it took in the worker come from the worker's own log
(sweep.py reads them): the client's numbers include its polling, which backs off up to 1 s.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/")          # the runner, as the image ships it
import test_runner as tr          # noqa: E402

SINKS = {
    # name: (settings file, destination_template that keeps the burst's keys apart from every suite's)
    "mongo": ("01_mongo_document.json", "e2e-burst-{env}"),
    "redis": ("05_redis_hash.json", "e2e-burst:{env}:config"),
    "s3":    ("08_s3_minio.json", "e2e-burst/{env}"),
}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def build_rules(settings: dict[str, list[dict]], sinks: list[str]) -> list[dict]:
    rules: list[dict] = []
    for name in sinks:
        file, template = SINKS[name]
        rules += tr._variant(settings[file], destination_template=template)
    return rules


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a burst of webhooks and time how they finish")
    parser.add_argument("--count", type=int, default=100, help="webhooks to send at once")
    parser.add_argument("--sinks", default="mongo,redis,s3", help=f"rules to send to, from {sorted(SINKS)}")
    parser.add_argument("--senders", type=int, default=32, help="threads sending and waiting (client side)")
    parser.add_argument("--settings-dir", default="/e2e/schemas/settings")
    parser.add_argument("--orchestrator", default=os.environ.get("ORCHESTRATOR_URL", "http://orchestrator-api:8000"))
    parser.add_argument("--gitea", default=os.environ.get("GITEA_URL", "http://gitea:3000"))
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for the whole burst")
    parser.add_argument("--out", default="", help="write the result here as JSON")
    args = parser.parse_args()

    sinks = [s for s in args.sinks.split(",") if s]
    unknown = [s for s in sinks if s not in SINKS]
    if unknown:
        print(f"unknown sink(s) {unknown}; choose from {sorted(SINKS)}", file=sys.stderr)
        return 2

    orchestrator = tr.OrchestratorClient(args.orchestrator, os.environ.get("INTERNAL_AUTH_SECRET", ""))
    gitea = tr.GiteaClient(args.gitea, os.environ.get("GITEA_ADMIN_USER", "demo-admin"),
                           os.environ.get("GITEA_ADMIN_PASSWORD", "VarTrack123"))

    settings = tr.load_settings(args.settings_dir)
    rules = build_rules(settings, sinks)
    for rule in rules:
        tr.reset_destination(rule)
    pushed = gitea.push_bundle(rules)
    commit_sha = gitea.get_head_sha(tr.CONFIG_REPO)
    warmed = tr.warm_schema(orchestrator, tr.DEMO_TENANT)
    if warmed["schema_commit"] != pushed or not warmed["rules_loaded"]:
        print(f"the schemas were not loaded at the pushed commit: {warmed}", file=sys.stderr)
        return 1

    datasources = [r["datasource"] for r in rules]
    started_at = time.time()
    t0 = time.monotonic()

    def one(i: int) -> dict:
        ds = datasources[i % len(datasources)]
        sent = time.monotonic()
        task_id = tr.trigger_webhook(orchestrator, ds, tr.DEMO_TENANT, commit_sha, args.gitea)
        accepted = time.monotonic()
        if not task_id:
            return {"i": i, "ds": ds, "state": "NOT_ACCEPTED", "sent": sent - t0, "accepted": accepted - t0}
        outcome = tr.wait_for_task(orchestrator, task_id, timeout=args.timeout)
        done = time.monotonic()
        return {"i": i, "ds": ds, "task": task_id, "state": outcome.state, "sent": sent - t0,
                "accepted": accepted - t0, "finished": done - t0}

    with ThreadPoolExecutor(max_workers=max(1, min(args.senders, args.count))) as pool:
        tasks = list(pool.map(one, range(args.count)))

    ok = [t for t in tasks if t["state"] == "SUCCEEDED"]
    wall = max((t.get("finished", 0) for t in tasks), default=0)
    latencies = [t["finished"] - t["accepted"] for t in ok]
    summary = {
        "count": args.count,
        "sinks": sinks,
        "started_at": started_at,
        "ok": len(ok),
        "not_ok": args.count - len(ok),
        "states": {s: sum(1 for t in tasks if t["state"] == s) for s in sorted({t["state"] for t in tasks})},
        "wall_s": round(wall, 3),
        "syncs_per_s": round(len(ok) / wall, 2) if wall else 0,
        "accept_to_seen_finished_s": {
            "median": round(statistics.median(latencies), 3) if latencies else None,
            "p90": round(percentile(latencies, 0.9), 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
    }
    print(json.dumps(summary))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({**summary, "tasks": tasks}, indent=1), encoding="utf-8")
    return 0 if not summary["not_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
