import os
import shutil
import site
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "start-nexus.ps1"


def _powershell():
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise unittest.SkipTest("PowerShell is required for launcher tests")


def _run_launcher(*args, env=None):
    return subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


class StartNexusLauncherTests(unittest.TestCase):
    def test_missing_project_venv_fails_clearly_without_starting_anything(self):
        with tempfile.TemporaryDirectory() as empty_repo:
            result = _run_launcher("-RepoRoot", empty_repo, "-NoBrowser")
        self.assertEqual(result.returncode, 1)
        self.assertIn("NEXUS STARTUP FAILED", result.stdout)
        self.assertIn("Python environment not found", result.stdout)
        self.assertNotIn("Control Center started", result.stdout)

    def test_unreachable_omniroute_without_command_fails_before_starting_nexus(self):
        with tempfile.TemporaryDirectory() as repo:
            venv.EnvBuilder(with_pip=False).create(Path(repo) / ".venv")
            env = os.environ.copy()
            python_paths = [str(ROOT), *site.getsitepackages()]
            if env.get("PYTHONPATH"):
                python_paths.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(python_paths)
            command_dir = Path(repo) / "commands"
            command_dir.mkdir()
            (command_dir / "codex.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            env["PATH"] = str(command_dir) + os.pathsep + env.get("PATH", "")
            omni_profile = Path(repo) / "omni-profile"
            omni_profile.mkdir()
            (omni_profile / "config.toml").write_text("model_provider = \"omniroute\"\n", encoding="utf-8")
            env["NEXUS_OMNIROUTE_CODEX_HOME"] = str(omni_profile)
            result = _run_launcher(
                "-RepoRoot", repo,
                "-OmniRoutePort", "20999",
                "-OmniRouteCommand", "no-such-omniroute-command-for-tests",
                "-NoBrowser",
                env=env,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("NEXUS STARTUP FAILED", result.stdout)
        self.assertIn("is not available", result.stdout)
        self.assertNotIn("Control Center started", result.stdout)

    def test_launcher_does_not_hardcode_the_repository_path(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users\\Vitor", source)
        self.assertNotIn("--reload", source)


if __name__ == "__main__":
    unittest.main()
