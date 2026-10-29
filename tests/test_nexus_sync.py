"""Focused safety-guard tests for scripts/nexus-sync.ps1 (P7 GitHub delivery
automation): it must never assume main as a PR base, never stage anything
beyond the authorized file list, and never touch a protected branch
directly. These tests never reach the network -- they only exercise the
guard rails that run before any git/gh mutation."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nexus-sync.ps1"


def _powershell():
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise unittest.SkipTest("PowerShell is required for nexus-sync tests")


def _run(args, env=None):
    return subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(path: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)


class NexusSyncGuardRailTests(unittest.TestCase):
    def test_refuses_a_repo_root_without_git(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run([
                "-Files", "a.txt", "-Message", "x", "-Base", "develop",
                "-RepoRoot", empty_dir,
            ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("NEXUS SYNC BLOCKED", result.stdout)
        self.assertIn("Not a Git repository root", result.stdout)

    def test_never_commits_or_pushes_directly_to_a_protected_branch(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            path = Path(repo_dir)
            _init_repo(path, "main")
            (path / "change.txt").write_text("x\n", encoding="utf-8")
            result = _run([
                "-Files", "change.txt", "-Message", "x", "-Base", "develop",
                "-Branch", "main", "-RepoRoot", repo_dir,
            ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("protected branch", result.stdout)
        self.assertIn("Branch: main", result.stdout)

    def test_never_assumes_main_as_the_pr_base(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            path = Path(repo_dir)
            _init_repo(path, "feature-x")
            (path / "change.txt").write_text("x\n", encoding="utf-8")
            result = _run([
                "-Files", "change.txt", "-Message", "x",
                "-Branch", "feature-x", "-RepoRoot", repo_dir,
            ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("A base branch is required", result.stdout)
        self.assertIn("never assumes main", result.stdout)
        self.assertNotIn("main", result.stdout.lower().split("never assumes ")[0])

    def test_base_and_working_branch_must_differ(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            path = Path(repo_dir)
            _init_repo(path, "feature-x")
            (path / "change.txt").write_text("x\n", encoding="utf-8")
            result = _run([
                "-Files", "change.txt", "-Message", "x", "-Base", "feature-x",
                "-Branch", "feature-x", "-RepoRoot", repo_dir,
            ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Base and working branch must differ", result.stdout)

    def test_refuses_to_stage_a_file_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            path = Path(repo_dir)
            _init_repo(path, "feature-x")
            result = _run([
                "-Files", "does-not-exist.txt", "-Message", "x", "-Base", "develop",
                "-Branch", "feature-x", "-RepoRoot", repo_dir,
            ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("do not exist on disk", result.stdout)
        self.assertIn("does-not-exist.txt", result.stdout)

    def test_blocks_with_an_exact_reason_when_github_cli_is_unavailable(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            path = Path(repo_dir)
            _init_repo(path, "feature-x")
            (path / "change.txt").write_text("x\n", encoding="utf-8")
            stripped_env = dict(os.environ)
            stripped_env["PATH"] = r"C:\Windows\System32;C:\Windows"
            result = _run([
                "-Files", "change.txt", "-Message", "x", "-Base", "develop",
                "-Branch", "feature-x", "-RepoRoot", repo_dir,
            ], env=stripped_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("GitHub CLI (gh) is not available on PATH", result.stdout)

    def test_source_never_merges_applies_or_force_pushes(self):
        # Strip comment-only lines first: the script documents, in a
        # comment, that it deliberately never force-pushes -- that mention
        # of "--force" is not a use of the flag.
        code_lines = [
            line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        ]
        source = "\n".join(code_lines)
        self.assertNotIn("--force", source)
        self.assertNotIn("git apply", source)
        self.assertNotIn("pr merge", source)
        self.assertNotIn("git merge", source)
        self.assertNotIn("git add -A", source)
        self.assertNotIn("git add .", source)

    def test_source_does_not_hardcode_a_repository_path(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users\\Vitor", source)


if __name__ == "__main__":
    unittest.main()
