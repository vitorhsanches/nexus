"""Tests for nexus/orchestration/js_dependency_prep.py (opt-in JS dependency
readiness for GO missions -- NEXUS_GO_JS_DEPENDENCY_READINESS).

Covers:
  - detection (npm-managed project vs. not vs. unreadable manifest)
  - real `npm ci --ignore-scripts` against a zero-dependency fixture
    (fast, deterministic, no network required)
  - test-tooling verification, including the "test command resolves via
    PATH, not node_modules/.bin" case (e.g. a `node -e ...` test script)
  - baseline test execution (pass and fail, neither ever raises)
  - node_modules propagation between worktrees

Real `npm`/`node` subprocess calls are used (matching the existing test
suite's philosophy of real `git` subprocess calls rather than mocks for
filesystem/process mechanics) but are skipped gracefully when npm is not
installed on the machine running the tests.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from nexus.orchestration.js_dependency_prep import (
    JsDependencyPrepError,
    copy_node_modules,
    detect_js_project,
    install_dependencies,
    run_baseline_tests,
    verify_test_tooling,
)

_NPM_AVAILABLE = shutil.which("npm") is not None
_skip_if_no_npm = unittest.skipUnless(_NPM_AVAILABLE, "npm is not installed on this machine")

# A real, zero-dependency npm project: `npm ci` needs no network and is
# near-instant, but still exercises the genuine npm toolchain end to end.
_ZERO_DEP_PACKAGE_JSON = {
    "name": "nexus-js-prep-fixture",
    "version": "1.0.0",
    "private": True,
    "scripts": {"test": "node -e \"process.exit(0)\""},
}
_ZERO_DEP_LOCKFILE = {
    "name": "nexus-js-prep-fixture",
    "version": "1.0.0",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {"": {"name": "nexus-js-prep-fixture", "version": "1.0.0"}},
}

_FAILING_TEST_PACKAGE_JSON = {
    **_ZERO_DEP_PACKAGE_JSON,
    "scripts": {"test": "node -e \"process.exit(1)\""},
}


def _write_npm_fixture(root: Path, package_json: dict, with_lockfile: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps(package_json), encoding="utf-8")
    if with_lockfile:
        (root / "package-lock.json").write_text(json.dumps(_ZERO_DEP_LOCKFILE), encoding="utf-8")


class DetectJsProjectTests(unittest.TestCase):
    def test_no_package_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(detect_js_project(tmp))

    def test_package_json_without_lockfile_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package.json").write_text(json.dumps(_ZERO_DEP_PACKAGE_JSON), encoding="utf-8")
            self.assertIsNone(detect_js_project(tmp))

    def test_package_json_and_lockfile_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_npm_fixture(Path(tmp), _ZERO_DEP_PACKAGE_JSON)
            project = detect_js_project(tmp)
            self.assertIsNotNone(project)
            self.assertEqual(project["manifest"]["name"], "nexus-js-prep-fixture")

    def test_corrupt_package_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package.json").write_text("{not valid json", encoding="utf-8")
            Path(tmp, "package-lock.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(JsDependencyPrepError) as ctx:
                detect_js_project(tmp)
            self.assertEqual(ctx.exception.code, "JS_MANIFEST_UNREADABLE")


@_skip_if_no_npm
class InstallDependenciesTests(unittest.TestCase):
    def test_real_npm_ci_succeeds_for_zero_dependency_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_npm_fixture(Path(tmp), _ZERO_DEP_PACKAGE_JSON)
            result = install_dependencies(tmp, timeout=60)
            self.assertTrue(result["passed"])
            self.assertEqual(result["exit_code"], 0)

    def test_missing_lockfile_fails_install(self):
        """npm ci (unlike npm install) refuses to run without a lockfile --
        confirms the function surfaces that as a clear failure, not a hang
        or a silent fallback to a different install strategy."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package.json").write_text(json.dumps(_ZERO_DEP_PACKAGE_JSON), encoding="utf-8")
            with self.assertRaises(JsDependencyPrepError) as ctx:
                install_dependencies(tmp, timeout=60)
            self.assertEqual(ctx.exception.code, "JS_DEPENDENCY_INSTALL_FAILED")

    def test_install_never_runs_lifecycle_scripts(self):
        """A `postinstall` script must never execute -- --ignore-scripts is
        unconditional, not a tunable. Verified by making postinstall write
        a marker file: if --ignore-scripts were ever dropped, this file
        would appear and the test would fail."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "SCRIPT_RAN.txt"
            package_json = {
                **_ZERO_DEP_PACKAGE_JSON,
                "scripts": {
                    **_ZERO_DEP_PACKAGE_JSON["scripts"],
                    "postinstall": f"node -e \"require('fs').writeFileSync({str(marker)!r}, 'x')\"",
                },
            }
            _write_npm_fixture(root, package_json)
            install_dependencies(tmp, timeout=60)
            self.assertFalse(marker.exists(), "postinstall ran despite --ignore-scripts")


class VerifyTestToolingTests(unittest.TestCase):
    def test_no_test_script_is_not_an_error(self):
        result = verify_test_tooling("/irrelevant", {"scripts": {}})
        self.assertIsNone(result["binary"])
        self.assertIsNone(result["binary_found"])

    def test_manifest_without_scripts_key_is_not_an_error(self):
        result = verify_test_tooling("/irrelevant", {})
        self.assertIsNone(result["binary"])

    def test_missing_binary_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {"scripts": {"test": "totally-fake-test-binary-xyz run"}}
            with self.assertRaises(JsDependencyPrepError) as ctx:
                verify_test_tooling(tmp, manifest)
            self.assertEqual(ctx.exception.code, "JS_TEST_TOOL_UNAVAILABLE")
            self.assertEqual(ctx.exception.detail["binary"], "totally-fake-test-binary-xyz")

    def test_binary_found_in_node_modules_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            bin_name = "vitest.cmd" if os.name == "nt" else "vitest"
            (bin_dir / bin_name).write_text("", encoding="utf-8")
            manifest = {"scripts": {"test": "vitest run"}}
            result = verify_test_tooling(tmp, manifest)
            self.assertTrue(result["binary_found"])
            self.assertEqual(result["binary_source"], "node_modules/.bin")

    @_skip_if_no_npm
    def test_binary_resolvable_via_path_is_accepted(self):
        """A test command whose binary is a system tool resolvable on PATH
        (e.g. `node` itself) is accepted even with no node_modules/.bin --
        the real invariant is "can this run", not "is it project-local"."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {"scripts": {"test": "node -e \"process.exit(0)\""}}
            result = verify_test_tooling(tmp, manifest)
            self.assertTrue(result["binary_found"])
            self.assertEqual(result["binary_source"], "PATH")


@_skip_if_no_npm
class RunBaselineTestsTests(unittest.TestCase):
    def test_passing_test_reports_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_npm_fixture(Path(tmp), _ZERO_DEP_PACKAGE_JSON)
            result = run_baseline_tests(tmp, timeout=60)
            self.assertTrue(result["passed"])

    def test_failing_test_reports_failed_without_raising(self):
        """A failing project test is evidence, not a preparation error --
        this function must never raise for a legitimately failing test."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_npm_fixture(Path(tmp), _FAILING_TEST_PACKAGE_JSON)
            result = run_baseline_tests(tmp, timeout=60)
            self.assertFalse(result["passed"])
            self.assertEqual(result["exit_code"], 1)


class CopyNodeModulesTests(unittest.TestCase):
    def test_copies_tree_between_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_wt = Path(tmp) / "source"
            target_wt = Path(tmp) / "target"
            source_wt.mkdir()
            target_wt.mkdir()
            nm = source_wt / "node_modules" / "some-pkg"
            nm.mkdir(parents=True)
            (nm / "index.js").write_text("module.exports = 1;", encoding="utf-8")

            copy_node_modules(str(source_wt), str(target_wt))

            copied = target_wt / "node_modules" / "some-pkg" / "index.js"
            self.assertTrue(copied.exists())
            self.assertEqual(copied.read_text(encoding="utf-8"), "module.exports = 1;")

    def test_missing_source_node_modules_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_wt = Path(tmp) / "source"
            target_wt = Path(tmp) / "target"
            source_wt.mkdir()
            target_wt.mkdir()
            with self.assertRaises(JsDependencyPrepError) as ctx:
                copy_node_modules(str(source_wt), str(target_wt))
            self.assertEqual(ctx.exception.code, "JS_DEPENDENCY_PROPAGATION_FAILED")


if __name__ == "__main__":
    unittest.main()
