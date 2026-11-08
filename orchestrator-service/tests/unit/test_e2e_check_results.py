"""
The gate on the e2e regression (e2e/check_results.py): CI reads the runner's report and fails unless every
suite ran and passed. A gate that lets a failing, a skipped or a missing suite through is the bug it exists
to close, so each of those is checked here.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

CHECK_PY = pathlib.Path(__file__).resolve().parents[3] / "e2e" / "check_results.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("e2e_check_results", CHECK_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def suite(n, status="pass", checks=None, error=None):
    return {"suite_id": n, "name": f"suite {n}", "status": status, "duration_ms": 1500.0,
            "checks": checks if checks is not None else [{"name": "a check", "passed": status == "pass", "detail": ""}],
            "error": error}


def report(statuses=None, total=None):
    statuses = statuses or ["pass"] * 23
    suites = [suite(i + 1, status) for i, status in enumerate(statuses)]
    return {
        "summary": {
            "total": len(suites) if total is None else total,
            "passed": statuses.count("pass"), "failed": statuses.count("fail"), "skipped": statuses.count("skip"),
        },
        "suites": suites,
    }


def write(tmp_path, body):
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def run(gate, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["check_results.py", *argv])
    return gate.main()


def test_a_complete_green_run_passes(gate, tmp_path, monkeypatch, capsys):
    assert run(gate, monkeypatch, write(tmp_path, report())) == 0
    assert "23/23 passed" in capsys.readouterr().out


def test_a_failed_suite_fails_the_run_and_is_named(gate, tmp_path, monkeypatch, capsys):
    statuses = ["pass"] * 23
    statuses[13] = "fail"
    body = report(statuses)
    body["suites"][13]["checks"] = [{"name": "database.password resolved", "passed": False, "detail": "[placeholder]"}]
    assert run(gate, monkeypatch, write(tmp_path, body)) == 1
    out = capsys.readouterr().out
    assert "suite 14 suite 14: fail" in out
    assert "database.password resolved: [placeholder]" in out        # the failing check, with what it found


def test_a_skipped_suite_is_not_a_pass(gate, tmp_path, monkeypatch):
    statuses = ["pass"] * 22 + ["skip"]
    assert run(gate, monkeypatch, write(tmp_path, report(statuses))) == 1


def test_a_run_that_lost_a_suite_is_not_a_pass(gate, tmp_path, monkeypatch, capsys):
    # every suite it ran passed, and the summary says so: it still ran 22 of 23
    assert run(gate, monkeypatch, write(tmp_path, report(["pass"] * 22))) == 1
    assert "22 suite(s) in the report, 23 expected" in capsys.readouterr().out


def test_a_suite_in_the_report_twice_is_not_a_pass(gate, tmp_path, monkeypatch):
    body = report()
    body["suites"][22] = dict(body["suites"][21])
    assert run(gate, monkeypatch, write(tmp_path, body)) == 1


def test_a_summary_that_disagrees_with_the_suites_does_not_hide_a_failure(gate, tmp_path, monkeypatch):
    body = report()
    body["suites"][3]["status"] = "fail"          # the summary still claims 23 passed
    assert run(gate, monkeypatch, write(tmp_path, body)) == 1


def test_no_report_is_exit_2_not_a_pass(gate, tmp_path, monkeypatch, capsys):
    assert run(gate, monkeypatch, str(tmp_path / "missing.json")) == 2
    assert "did not get as far as writing one" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"summary": {}}', '{"suites": []}'])
def test_an_unreadable_report_is_exit_2(gate, tmp_path, monkeypatch, text):
    path = tmp_path / "benchmark.json"
    path.write_text(text, encoding="utf-8")
    assert run(gate, monkeypatch, str(path)) == 2


def test_the_expected_count_can_be_given(gate, tmp_path, monkeypatch):
    path = write(tmp_path, report(["pass"] * 3))
    assert run(gate, monkeypatch, path, "--expect", "3") == 0
    assert run(gate, monkeypatch, path) == 1


def test_the_table_goes_to_the_github_step_summary(gate, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run(gate, monkeypatch, write(tmp_path, report(["pass"] * 22 + ["fail"])))
    text = summary.read_text(encoding="utf-8")
    assert "22/23 passed" in text and "| 23 | suite 23 | fail |" in text


def test_a_suite_that_raised_shows_its_traceback(gate, tmp_path, monkeypatch, capsys):
    body = report()
    body["suites"][0]["status"] = "fail"
    body["suites"][0]["error"] = "Traceback (most recent call last):\n  RuntimeError: schema warm-up refused: 500"
    run(gate, monkeypatch, write(tmp_path, body))
    assert "schema warm-up refused: 500" in capsys.readouterr().out


def test_a_console_in_a_legacy_code_page_does_not_stop_the_gate(gate, tmp_path, monkeypatch):
    """Suite names have arrows in them. On a Windows console in cp1255 (or cp1252) printing one raised, and
    the gate died instead of answering."""
    import io

    body = report()
    body["suites"][19]["name"] = "branch_map: develop → staging collection"
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1255", errors="strict")
    monkeypatch.setattr("sys.stdout", console)
    assert run(gate, monkeypatch, write(tmp_path, body)) == 0
    console.flush()
    assert "develop → staging collection".encode("utf-8") in raw.getvalue()
