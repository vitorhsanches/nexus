import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "start-nexus.ps1"


def _powershell():
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise unittest.SkipTest("PowerShell is required for launcher tests")


def _run_launcher(*args):
    return subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=120,
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
        result = _run_launcher(
            "-OmniRoutePort", "20999",
            "-OmniRouteCommand", "no-such-omniroute-command-for-tests",
            "-NoBrowser",
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
