"""Tests for the opt-in `prepare_dependencies` wiring in
nexus/orchestration/integration.py::run_integrated_plan() (NEXUS_GO_JS_
DEPENDENCY_READINESS).

Pattern matches tests/test_phase4_4_integration.py: real git repositories,
real `npm` subprocess calls (a zero-dependency fixture -- fast, no network),
`execute_progressively` and `record_checkpoint` mocked to isolate from real
Worker/Reviewer agent execution. Skipped gracefully when npm is not
installed on the machine running the tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexus.orchestration.integration import IntegrationError, run_integrated_plan

_NPM_AVAILABLE = shutil.which("npm") is not None
_skip_if_no_npm = unittest.skipUnless(_NPM_AVAILABLE, "npm is not installed on this machine")

_PASSING_PACKAGE_JSON = {
    "name": "nexus-go-js-fixture",
    "version": "1.0.0",
    "private": True,
    "scripts": {"test": "node -e \"process.exit(0)\""},
}
_LOCKFILE = {
    "name": "nexus-go-js-fixture",
    "version": "1.0.0",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {"": {"name": "nexus-go-js-fixture", "version": "1.0.0"}},
}


def _git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def _make_repo(tmp_dir: str, package_json: dict | None = None, with_lockfile: bool = True) -> str:
    repo = os.path.join(tmp_dir, "primary")
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    Path(repo, "README.md").write_text("hello\n", encoding="utf-8")
    if package_json is not None:
        # Real-world JS projects always gitignore node_modules (FinaSync's
        # own .gitignore does too); without this, an untracked
        # node_modules/ would be mistaken by the integration pipeline's
        # diff detection for a real Worker-authored source change.
        Path(repo, ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        Path(repo, "package.json").write_text(json.dumps(package_json), encoding="utf-8")
        if with_lockfile:
            Path(repo, "package-lock.json").write_text(json.dumps(_LOCKFILE), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--no-verify", "-m", "init")
    return repo


def _make_workers(n: int) -> list[dict]:
    return [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": f"worker {i}",
            "reason": "test",
            "depends_on": [],
        }
        for i in range(n)
    ]


def _completed_outcome(worktree: str) -> dict:
    return {
        "status": "COMPLETED",
        "worker": {"worktree": worktree, "agent_id": "agent-1", "output": "done"},
        "review": {"reviewer_id": "rev-1"},
    }


@_skip_if_no_npm
class PrepareDependenciesDisabledByDefaultTests(unittest.TestCase):
    """Default GO behavior (prepare_dependencies omitted) is untouched,
    even for a real npm-managed project."""

    def test_default_false_never_touches_npm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, _PASSING_PACKAGE_JSON)
            captured_repo_paths = []

            def fake_execute_progressively(**kwargs):
                captured_repo_paths.append(kwargs["repo"])
                return _completed_outcome(kwargs["repo"])

            with (
                patch("nexus.orchestration.integration.execute_progressively",
                      side_effect=fake_execute_progressively),
                patch("nexus.orchestration.integration.record_checkpoint"),
                patch("nexus.orchestration.integration.install_dependencies") as mock_install,
            ):
                result = run_integrated_plan(
                    run_id="RUN-DEFAULT",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="task",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                )

            mock_install.assert_not_called()
            self.assertEqual(len(result), 1)
            # No node_modules materialized anywhere -- nothing ran npm ci.
            self.assertFalse(Path(captured_repo_paths[0], "node_modules").exists())


@_skip_if_no_npm
class PrepareDependenciesSuccessTests(unittest.TestCase):
    """Opted-in, real npm project: dependencies prepared and propagated to
    the Worker's sub-worktree before execute_progressively runs."""

    def test_zero_dependency_project_completes_normally(self):
        """A real, legitimate zero-dependency project (no third-party
        packages at all) must not be treated as a readiness failure: `npm
        ci` succeeds, the declared `node -e ...` test script resolves via
        PATH (nothing to install), and the Worker still runs."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, _PASSING_PACKAGE_JSON)

            with (
                patch("nexus.orchestration.integration.execute_progressively",
                      side_effect=lambda **kw: _completed_outcome(kw["repo"])),
                patch("nexus.orchestration.integration.record_checkpoint"),
            ):
                result = run_integrated_plan(
                    run_id="RUN-JS-ZERO-DEP",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="task",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                    prepare_dependencies=True,
                )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["status"], "COMPLETED")

    def test_node_modules_available_in_worker_worktree_before_execution(self):
        """A project WITH dependencies (simulated: install_dependencies is
        the only mocked step, standing in for `npm ci` actually fetching
        packages -- real third-party registry access is out of scope for
        an offline, deterministic test) gets node_modules propagated into
        the Worker's own sub-worktree before the Worker runs. detect/
        verify/copy all run for real."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, _PASSING_PACKAGE_JSON)
            seen_node_modules_present = []

            def fake_execute_progressively(**kwargs):
                repo_path = kwargs["repo"]
                seen_node_modules_present.append(
                    (Path(repo_path, "node_modules", "some-pkg", "index.js").read_text(encoding="utf-8")
                     if Path(repo_path, "node_modules", "some-pkg", "index.js").exists() else None)
                )
                return _completed_outcome(repo_path)

            def fake_install_dependencies(worktree, **kwargs):
                nm = Path(worktree, "node_modules", "some-pkg")
                nm.mkdir(parents=True)
                (nm / "index.js").write_text("module.exports = 1;", encoding="utf-8")
                return {"output_tail": "added 1 package"}

            with (
                patch("nexus.orchestration.integration.execute_progressively",
                      side_effect=fake_execute_progressively),
                patch("nexus.orchestration.integration.record_checkpoint"),
                patch("nexus.orchestration.integration.install_dependencies",
                      side_effect=fake_install_dependencies),
            ):
                result = run_integrated_plan(
                    run_id="RUN-JS-OK",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="task",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                    prepare_dependencies=True,
                )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["status"], "COMPLETED")
            self.assertEqual(
                seen_node_modules_present[0], "module.exports = 1;",
                "Worker sub-worktree did not have the prepared node_modules propagated",
            )


@_skip_if_no_npm
class PrepareDependenciesEarlyFailureTests(unittest.TestCase):
    """An explicit request that cannot be honored fails BEFORE the
    expensive Worker ever launches, with the correct TOOL_FAILURE/
    environmental classification -- never a silent bypass, never
    misclassified as a model capability problem."""

    def test_non_js_project_raises_before_worker_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, package_json=None)  # no package.json at all

            with (
                patch("nexus.orchestration.integration.execute_progressively") as mock_ep,
                patch("nexus.orchestration.integration.record_checkpoint"),
            ):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-NOT-JS",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                        prepare_dependencies=True,
                    )

            mock_ep.assert_not_called()
            self.assertEqual(ctx.exception.code, "JS_PROJECT_NOT_DETECTED")
            self.assertEqual(ctx.exception.run_status, "BLOCKED")
            failure = ctx.exception.payload["failure"]
            self.assertEqual(failure["stage"], "DEPENDENCY_PREPARATION")
            self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
            self.assertEqual(failure["category"], "environmental")
            self.assertNotEqual(failure["failure_class"], "CAPABILITY_FAILURE")

    def test_missing_lockfile_raises_before_worker_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, _PASSING_PACKAGE_JSON, with_lockfile=False)

            with (
                patch("nexus.orchestration.integration.execute_progressively") as mock_ep,
                patch("nexus.orchestration.integration.record_checkpoint"),
            ):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-NO-LOCK",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                        prepare_dependencies=True,
                    )

            mock_ep.assert_not_called()
            self.assertEqual(ctx.exception.code, "JS_PROJECT_NOT_DETECTED")

    def test_undeclared_test_binary_raises_before_worker_launches(self):
        package_json = {
            **_PASSING_PACKAGE_JSON,
            "scripts": {"test": "totally-fake-test-binary-xyz run"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, package_json)

            with (
                patch("nexus.orchestration.integration.execute_progressively") as mock_ep,
                patch("nexus.orchestration.integration.record_checkpoint"),
            ):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-NO-TOOL",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                        prepare_dependencies=True,
                    )

            mock_ep.assert_not_called()
            self.assertEqual(ctx.exception.code, "JS_TEST_TOOL_UNAVAILABLE")
            failure = ctx.exception.payload["failure"]
            self.assertEqual(failure["failure_class"], "TOOL_FAILURE")

    def test_integration_worktree_cleaned_up_after_early_failure(self):
        """Early dependency-prep failure must not leak a worktree/branch on
        the primary repo -- same cleanup guarantee as every other
        early-failure path in this module."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp, package_json=None)

            with (
                patch("nexus.orchestration.integration.execute_progressively"),
                patch("nexus.orchestration.integration.record_checkpoint"),
            ):
                with self.assertRaises(IntegrationError):
                    run_integrated_plan(
                        run_id="RUN-CLEANUP",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                        prepare_dependencies=True,
                    )

            worktrees = subprocess.run(
                ["git", "worktree", "list"], cwd=repo, check=True,
                capture_output=True, text=True,
            ).stdout
            self.assertEqual(worktrees.count("\n"), 1, f"leaked worktree(s):\n{worktrees}")


if __name__ == "__main__":
    unittest.main()
