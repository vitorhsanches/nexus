"""F3.6A -- single-command runner for the Dev Mode integration safety smoke.

Usage (from the repository root, in an activated .venv):

    python scripts/run_integration_safety_smoke.py

Runs tests/test_integration_safety_smoke.py (the four real-Git-repository
safety scenarios: successful Apply, integration conflict, stale review,
duplicate Apply) and prints a compact PASS/FAIL/BLOCKED report with one
line of evidence per scenario, followed by the full pytest output for
detail. No PowerShell/browser interaction is required -- this is a plain
Python entry point an operator (or an agent) runs once, and it exits
non-zero if anything did not PASS.

This never calls a model/App-Server: the scenarios exercise deterministic
Git plumbing (nexus/dev/integration.py) only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/test_integration_safety_smoke.py"

_SCENARIOS = {
    "test_scenario_a_successful_apply_end_to_end": "A - Successful Apply",
    "test_scenario_b_integration_conflict_is_rejected_and_destination_preserved": "B - Integration conflict",
    "test_scenario_c_stale_review_is_blocked_before_any_commit": "C - Stale review",
    "test_scenario_d_duplicate_apply_is_rejected_idempotently": "D - Duplicate Apply",
}


class _ResultCollector:
    def __init__(self):
        self.results: dict[str, dict] = {}

    def pytest_runtest_logreport(self, report):
        if report.when not in ("call", "setup"):
            if report.when == "setup" and report.outcome != "failed":
                return
        name = report.nodeid.split("::")[-1]
        if name not in _SCENARIOS:
            return
        if report.when == "setup" and report.outcome == "failed":
            self.results[name] = {"outcome": "BLOCKED", "reason": str(report.longrepr)}
        elif report.when == "call":
            if report.outcome == "passed":
                self.results[name] = {"outcome": "PASS", "reason": "all assertions in the scenario held"}
            else:
                self.results[name] = {"outcome": "FAIL", "reason": str(report.longrepr).splitlines()[-1] if report.longrepr else "assertion failed"}


def main() -> int:
    collector = _ResultCollector()
    exit_code = pytest.main(
        [str(REPO_ROOT / TEST_FILE), "-vv", "-p", "no:cacheprovider"],
        plugins=[collector],
    )

    print("\n" + "=" * 78)
    print("F3.6A -- Integration Safety Smoke Report")
    print("=" * 78)
    overall_ok = True
    for test_name, label in _SCENARIOS.items():
        result = collector.results.get(test_name, {"outcome": "BLOCKED", "reason": "test did not report a result (collection error?)"})
        outcome = result["outcome"]
        if outcome != "PASS":
            overall_ok = False
        print(f"[{outcome:7}] {label}")
        print(f"          {result['reason']}")
    print("=" * 78)
    print("Repository integrity: each scenario asserts `git status --porcelain`")
    print("is empty and HEAD matches the expected commit on its own disposable")
    print("destination repository after every attempt (see test file for the")
    print("exact assertions) -- no shared/real Nexus repository is touched.")
    print("=" * 78)
    print("Overall: " + ("PASS" if overall_ok and exit_code == 0 else "FAIL"))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
