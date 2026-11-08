#!/usr/bin/env python3
"""
Fail unless the e2e regression ran every suite and every suite passed.

    python3 e2e/check_results.py [e2e/results/benchmark.json] [--expect 23]

Reads the report the test runner writes (init/test_runner.py). Exit status: 0 when the run is complete and
green, 1 when a suite failed or was skipped or fewer/more suites ran than expected, 2 when there is no
usable report (the stack never got as far as running the suites). The per-suite table, and each failed
check with what it found, go to stdout and, when it is set, to $GITHUB_STEP_SUMMARY.

The report is not kept in git: a stale one would let a broken run pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

EXPECTED_SUITES = 23   # change together with the suites in init/test_runner.py


def num(suite: dict) -> str:
    return str(suite.get("suite_id", "?")).rjust(2, "0")


def load(path: Path) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"no report at {path}: the regression did not get as far as writing one") from None
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path} is not a readable report: {exc}") from None
    if (not isinstance(report, dict) or not isinstance(report.get("summary"), dict)
            or not isinstance(report.get("suites"), list)):
        raise SystemExit(f"{path} is not a regression report (no summary and suites)")
    return report


def problems(report: dict, expect: int) -> list[str]:
    """Why this run is not a pass, one line each; empty when it is."""
    summary, suites = report["summary"], report["suites"]
    found = []
    if summary.get("total") != expect or len(suites) != expect:
        found.append(f"{len(suites)} suite(s) in the report, {expect} expected")
    ids = [s.get("suite_id") for s in suites]
    if len(set(ids)) != len(ids):
        found.append("a suite is in the report twice")
    for suite in suites:
        if suite.get("status") != "pass":
            found.append(f"suite {num(suite)} {suite.get('name')}: {suite.get('status')}")
    if summary.get("failed"):
        found.append(f"{summary['failed']} suite(s) failed")
    if summary.get("skipped"):
        found.append(f"{summary['skipped']} suite(s) skipped")
    return found


def render(report: dict, why: list[str]) -> str:
    summary = report["summary"]
    lines = [
        f"### e2e regression: {summary.get('passed')}/{summary.get('total')} passed",
        "",
        "| # | Suite | Status | Checks | Time |",
        "|---|---|---|---|---|",
    ]
    for suite in report["suites"]:
        checks = suite.get("checks") or []
        passed = sum(1 for c in checks if c.get("passed"))
        lines.append(f"| {num(suite)} | {suite.get('name')} | {suite.get('status')} | "
                     f"{passed}/{len(checks)} | {suite.get('duration_ms', 0) / 1000:.1f}s |")
    failing = [(s, c) for s in report["suites"] for c in s.get("checks") or [] if not c.get("passed")]
    if failing:
        lines += ["", "#### Failed checks", ""]
        for suite, check in failing:
            detail = f": {check['detail']}" if check.get("detail") else ""
            lines.append(f"- suite {num(suite)}: {check.get('name')}{detail}")
    for suite in report["suites"]:
        if suite.get("error"):
            lines += ["", f"#### Suite {num(suite)} raised", "", "```", str(suite["error"]).strip()[-1500:], "```"]
    if why:
        lines += ["", "#### Not a pass", ""] + [f"- {line}" for line in why]
    return "\n".join(lines) + "\n"


def main() -> int:
    # Suite names have arrows in them; a console in a legacy code page (cp1252, cp1255 …) cannot print
    # those, and the gate must not die on how its table is shown.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", nargs="?", default="e2e/results/benchmark.json")
    parser.add_argument("--expect", type=int, default=EXPECTED_SUITES)
    args = parser.parse_args()
    try:
        report = load(Path(args.report))
    except SystemExit as exc:
        print(f"e2e: {exc}", file=sys.stderr)
        return 2
    why = problems(report, args.expect)
    text = render(report, why)
    print(text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main())
