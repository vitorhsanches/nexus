"""
tests/test_git_scoped_trust.py

Process-scoped Git trust for Nexus agents that inspect an assigned repository
or worktree.

Sandboxed agents run under a different Windows identity than the host user
that owns the assigned worktree, so Git refuses it as "dubious ownership".
with_scoped_git_trust() adds one exact safe.directory entry as command-scope
configuration. These tests use real Git. The ownership failure is reproduced
with Git's own GIT_TEST_ASSUME_DIFFERENT_OWNER hook, so no second Windows
account is needed.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.process_env import (
    GIT_SAFE_DIRECTORY_KEY,
    command_argument,
    with_scoped_git_trust,
)

GIT = shutil.which("git")
OWNERSHIP_HOOK = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}


def _git(args, cwd, env):
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class _GitRepoCase(unittest.TestCase):
    """Creates disposable repositories in a plain directory, like the integration root."""

    def setUp(self):
        if not GIT:
            self.skipTest("git is not available")
        # Use the user's LOCALAPPDATA temp area, whose real path contains a
        # space (the default %TEMP% is an 8.3 short name without one).
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "Temp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="nexus_git_trust_", dir=str(base))
        self.root = Path(self._tmp.name)
        self.repo = self._make_repo("repo")
        self.sibling = self._make_repo("sibling")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_repo(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        base_env = {**os.environ}
        _git(["init", "-q"], path, base_env)
        _git(
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "--allow-empty", "-m", "init"],
            path,
            base_env,
        )
        return path

    def _host_env(self, **extra):
        env = {**os.environ, **OWNERSHIP_HOOK}
        env.update(extra)
        return env

    def _require_ownership_hook(self):
        """Skip unless this Git build reproduces the dubious-ownership error."""
        probe = _git(["status", "--short"], self.repo, self._host_env())
        if "dubious ownership" not in probe.stderr:
            self.skipTest("this Git build does not simulate dubious ownership")


class ScopedTrustGitBehaviourTests(_GitRepoCase):
    # CASE A: exact trust makes the assigned repository usable.
    def test_exact_trust_resolves_dubious_ownership(self):
        self._require_ownership_hook()
        env = with_scoped_git_trust(self._host_env(), str(self.repo))

        status = _git(["status", "--short"], self.repo, env)
        rev_parse = _git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo, env)
        diff = _git(["diff", "--stat"], self.repo, env)

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(rev_parse.returncode, 0, rev_parse.stderr)
        self.assertEqual(diff.returncode, 0, diff.stderr)

    # CASE B: a sibling repository does not gain trust from the helper.
    def test_sibling_repository_is_not_trusted(self):
        self._require_ownership_hook()
        env = with_scoped_git_trust(self._host_env(), str(self.repo))

        sibling = _git(["status", "--short"], self.sibling, env)

        self.assertNotEqual(sibling.returncode, 0)
        self.assertIn("dubious ownership", sibling.stderr)

    # CASE C: existing command-scope entries are kept and the new one is appended.
    def test_existing_command_config_is_preserved_and_appended(self):
        self._require_ownership_hook()
        base = self._host_env(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="user.name",
            GIT_CONFIG_VALUE_0="kept-by-caller",
        )
        env = with_scoped_git_trust(base, str(self.repo))

        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "user.name")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "kept-by-caller")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], GIT_SAFE_DIRECTORY_KEY)

        name = _git(["config", "--get", "user.name"], self.repo, env)
        status = _git(["status", "--short"], self.repo, env)
        self.assertEqual(name.stdout.strip(), "kept-by-caller")
        self.assertEqual(status.returncode, 0, status.stderr)

    # CASE D: a path containing spaces is trusted exactly.
    def test_path_with_spaces_is_trusted_exactly(self):
        self._require_ownership_hook()
        self.assertIn(" ", str(self.repo), "test expects a path containing spaces")
        env = with_scoped_git_trust(self._host_env(), str(self.repo))

        expected = os.path.normpath(str(self.repo)).replace(os.sep, "/")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], expected)

        status = _git(["status", "--short"], self.repo, env)
        self.assertEqual(status.returncode, 0, status.stderr)


class ScopedTrustLinkedWorktreeTests(_GitRepoCase):
    def test_linked_worktree_trusted_by_its_own_path(self):
        self._require_ownership_hook()
        worktree = self.root / "worker-0"
        # Setup runs without the ownership hook; the hook applies only to the
        # Git calls whose trust is under test.
        _git(["worktree", "add", "-q", "-b", "wt-branch", str(worktree)], self.repo, dict(os.environ))

        env = with_scoped_git_trust(self._host_env(), str(worktree))
        status = _git(["status", "--short"], worktree, env)
        self.assertEqual(status.returncode, 0, status.stderr)


# CASE E: the helper itself never writes wildcard, global or system trust.
class ScopedTrustSafetyTests(unittest.TestCase):
    def test_wildcard_path_is_rejected(self):
        with self.assertRaises(ValueError):
            with_scoped_git_trust({}, "C:\\*")

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ValueError):
            with_scoped_git_trust({}, "relative\\repo")

    def test_malformed_existing_count_is_rejected(self):
        with self.assertRaises(ValueError):
            with_scoped_git_trust({"GIT_CONFIG_COUNT": "many"}, "C:\\fake\\repo")

    def test_no_global_or_system_config_keys_are_written(self):
        original = {"PATH": "x", "GIT_CONFIG_COUNT": "0"}
        before = dict(original)

        env = with_scoped_git_trust(original, "C:\\fake\\repo")

        self.assertEqual(original, before, "helper must not mutate its input")
        forbidden = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
        self.assertFalse(forbidden & set(env))
        values = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")]
        self.assertNotIn("*", values)
        self.assertEqual(values, ["C:/fake/repo"])

    def test_command_argument_reads_flag_value(self):
        self.assertEqual(command_argument(["x", "-C", "C:\\wt"], "-C"), "C:\\wt")
        with self.assertRaises(ValueError):
            command_argument(["x"], "-C")


# Launch points: the environment handed to Popen carries exactly the assigned path.
class LaunchPointTrustTests(unittest.TestCase):
    WORKTREE = r"C:\fake\linked-worktree\worker-0"

    def test_reviewer_launcher_trusts_only_the_reviewed_worktree(self):
        from nexus.dispatchers import review

        command = ["codex.exe", "exec", "-C", self.WORKTREE, "--sandbox", "read-only", "prompt"]
        with patch.object(review, "codex_subprocess_env", return_value={}), patch.object(
            review.subprocess, "Popen"
        ) as popen:
            review._default_process_launcher(command)

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], GIT_SAFE_DIRECTORY_KEY)
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "C:/fake/linked-worktree/worker-0")

    def test_worker_launch_trusts_only_its_assigned_repo(self):
        from nexus.dispatchers import omniroute

        with patch.object(omniroute, "codex_subprocess_env", return_value={}), patch.object(
            omniroute.subprocess, "Popen"
        ) as popen, patch.object(omniroute, "update_agent_execution"), patch.object(
            omniroute, "update_agent_status"
        ), patch.object(omniroute, "create_agent", return_value="AGENT-TRUST01"):
            popen.side_effect = OSError("stop after capturing env")
            omniroute.run_omniroute_worker(
                run_id="RUN-TRUST",
                repo=self.WORKTREE,
                task="t",
                model="cc/claude-sonnet-5-low",
            )

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "C:/fake/linked-worktree/worker-0")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")


if __name__ == "__main__":
    unittest.main()
