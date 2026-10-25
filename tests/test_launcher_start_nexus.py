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


def _launcher_env(repo, *, install_codex, install_claude=False, omni_profile=False, omni_marker=False):
    """Project venv and an isolated machine: LOCALAPPDATA, USERPROFILE and PATH.

    Codex is placed only where Nexus resolves it at runtime
    (%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\codex.exe), never on PATH.
    Claude is selected explicitly through NEXUS_CLAUDE_PATH. PATH holds only the
    system directory, so a real Claude/OmniRoute install can never leak in.
    """
    repo = Path(repo)
    venv.EnvBuilder(with_pip=False).create(repo / ".venv")
    env = os.environ.copy()
    python_paths = [str(ROOT), *site.getsitepackages()]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    local_app_data = repo / "localappdata"
    local_app_data.mkdir()
    if install_codex:
        build_dir = local_app_data / "OpenAI" / "Codex" / "bin" / "test-build"
        build_dir.mkdir(parents=True)
        (build_dir / "codex.exe").write_bytes(b"")
    env["LOCALAPPDATA"] = str(local_app_data)
    home = repo / "home"
    home.mkdir()
    env["USERPROFILE"] = str(home)
    env.pop("NEXUS_CLAUDE_PATH", None)
    if install_claude:
        claude = repo / "tools" / "claude.exe"
        claude.parent.mkdir()
        claude.write_bytes(b"")
        env["NEXUS_CLAUDE_PATH"] = str(claude)
    commands = repo / "commands"
    commands.mkdir()
    env["PATH"] = str(commands) + os.pathsep + os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if omni_marker:
        # A fake OmniRoute command: if the launcher ever runs it, it leaves a marker.
        marker = repo / "omniroute-was-run.txt"
        (commands / "omniroute.cmd").write_text(f"@echo ran> \"{marker}\"\r\n", encoding="utf-8")
    omni_home = repo / "omni-profile"
    if omni_profile:
        omni_home.mkdir()
        (omni_home / "config.toml").write_text("model_provider = \"omniroute\"\n", encoding="utf-8")
    env["NEXUS_OMNIROUTE_CODEX_HOME"] = str(omni_home)
    env.pop("NEXUS_DEV_EXECUTION_ENABLED", None)
    return env


class StartNexusLauncherTests(unittest.TestCase):
    def test_missing_project_venv_fails_clearly_without_starting_anything(self):
        with tempfile.TemporaryDirectory() as empty_repo:
            result = _run_launcher("-RepoRoot", empty_repo, "-NoBrowser")
        self.assertEqual(result.returncode, 1)
        self.assertIn("NEXUS STARTUP FAILED", result.stdout)
        self.assertIn("Python environment not found", result.stdout)
        self.assertNotIn("Control Center started", result.stdout)

    # -- default start: native-first, no OmniRoute -------------------------------

    def test_default_start_needs_no_omniroute_command_service_or_profile(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True, install_claude=True, omni_marker=True)
            result = _run_launcher("-RepoRoot", repo, "-OmniRoutePort", "20999", "-PreflightOnly", "-NoBrowser", env=env)
            marker_exists = (Path(repo) / "omniroute-was-run.txt").exists()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Codex CLI available", result.stdout)
        self.assertIn("Claude Code CLI available", result.stdout)
        self.assertIn("OmniRoute      DISABLED", result.stdout)
        self.assertIn("Codex Native   AVAILABLE", result.stdout)
        self.assertIn("Claude Native  AVAILABLE", result.stdout)
        self.assertNotIn("OmniRoute online", result.stdout)
        self.assertNotIn("profile", result.stdout.lower().replace("omniroute profile", ""))
        self.assertFalse(marker_exists, "OmniRoute must not be started unless explicitly enabled")
        self.assertNotIn("NEXUS STARTUP FAILED", result.stdout)

    def test_missing_codex_and_claude_only_warn_and_never_block_startup(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=False, install_claude=False)
            result = _run_launcher("-RepoRoot", repo, "-PreflightOnly", "-NoBrowser", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARN] Codex CLI was not found", result.stdout)
        self.assertIn("[WARN] Claude Code CLI (claude.exe) was not found", result.stdout)
        self.assertIn("No native backend is available", result.stdout)
        self.assertIn("Codex Native   NOT FOUND", result.stdout)
        self.assertIn("Claude Native  NOT FOUND", result.stdout)
        self.assertNotIn("NEXUS STARTUP FAILED", result.stdout)

    def test_one_native_backend_missing_is_reported_without_choosing_the_other(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True, install_claude=False)
            result = _run_launcher("-RepoRoot", repo, "-PreflightOnly", "-NoBrowser", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Codex Native   AVAILABLE", result.stdout)
        self.assertIn("Claude Native  NOT FOUND", result.stdout)
        self.assertNotIn("No native backend is available", result.stdout)
        self.assertNotIn("NEXUS_DEV_BACKEND", result.stdout)

    def test_a_claude_cmd_shim_alone_is_not_treated_as_an_available_claude(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True)
            (Path(repo) / "commands" / "claude.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
            result = _run_launcher("-RepoRoot", repo, "-PreflightOnly", "-NoBrowser", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Claude Native  NOT FOUND", result.stdout)

    def test_codex_only_on_path_is_not_reported_as_available(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=False)
            (Path(repo) / "commands" / "codex.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
            result = _run_launcher("-RepoRoot", repo, "-PreflightOnly", "-NoBrowser", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARN] Codex CLI was not found where Nexus launches it", result.stdout)
        self.assertNotIn("Codex CLI available", result.stdout)

    def test_development_gate_stays_opt_in_and_is_reported(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True)
            default = _run_launcher("-RepoRoot", repo, "-PreflightOnly", "-NoBrowser", env=env)
            enabled = _run_launcher("-RepoRoot", repo, "-PreflightOnly", "-EnableDevelopment", "-NoBrowser", env=env)
        self.assertIn("Development execution remains disabled", default.stdout)
        self.assertIn("Development    execution disabled", default.stdout)
        self.assertIn("Development execution explicitly enabled", enabled.stdout)
        self.assertIn("Development    EXECUTION ENABLED", enabled.stdout)

    # -- explicit OmniRoute ------------------------------------------------------

    def test_enabling_omniroute_still_requires_its_profile(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True, omni_profile=False)
            result = _run_launcher("-RepoRoot", repo, "-EnableOmniRoute", "-PreflightOnly", "-NoBrowser", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("The dedicated Codex OmniRoute profile is missing", result.stdout)

    def test_enabling_omniroute_still_requires_codex(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=False, omni_profile=True)
            result = _run_launcher("-RepoRoot", repo, "-EnableOmniRoute", "-PreflightOnly", "-NoBrowser", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Codex CLI was not found where Nexus launches it", result.stdout)
        self.assertNotIn("Codex CLI available", result.stdout)

    def test_unreachable_omniroute_without_command_fails_before_starting_nexus(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True, omni_profile=True)
            result = _run_launcher(
                "-RepoRoot", repo,
                "-EnableOmniRoute",
                "-OmniRoutePort", "20999",
                "-OmniRouteCommand", "no-such-omniroute-command-for-tests",
                "-NoBrowser",
                env=env,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("NEXUS STARTUP FAILED", result.stdout)
        self.assertIn("Codex CLI available", result.stdout)
        self.assertIn("is not available", result.stdout)
        self.assertNotIn("Control Center started", result.stdout)

    def test_enabled_omniroute_preflight_reports_but_does_not_start_it(self):
        with tempfile.TemporaryDirectory() as repo:
            env = _launcher_env(repo, install_codex=True, omni_profile=True, omni_marker=True)
            result = _run_launcher("-RepoRoot", repo, "-EnableOmniRoute", "-OmniRoutePort", "20999",
                                   "-PreflightOnly", "-NoBrowser", env=env)
            marker_exists = (Path(repo) / "omniroute-was-run.txt").exists()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Codex OmniRoute profile available", result.stdout)
        self.assertIn("OmniRoute      OFFLINE (would be started)", result.stdout)
        self.assertFalse(marker_exists)

    # -- static guarantees -------------------------------------------------------

    def test_launcher_does_not_hardcode_the_repository_path(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users\\Vitor", source)
        self.assertNotIn("--reload", source)

    def test_launcher_never_selects_a_backend_or_enables_integration(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        for forbidden in ("NEXUS_DEV_INTEGRATION_ENABLED", "NEXUS_DEV_BACKEND", "codex_native", "claude_native"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
