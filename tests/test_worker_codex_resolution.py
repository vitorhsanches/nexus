"""
tests/test_worker_codex_resolution.py

Regression tests for the OmniRoute Worker's Codex executable resolution.

The Worker wrapper must not depend on `codex` being on PATH. It resolves the
installed Codex CLI with the same rule as the Manager and Review dispatchers:
the newest %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\*\\codex.exe by modification time.

These tests run the real helper script under PowerShell with a temporary
LOCALAPPDATA. They never launch Codex or a model.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "nexus" / "runtime" / "multi-agent-development-manager" / "scripts"
HELPER = SCRIPTS / "codex-resolution.ps1"
WRAPPER = SCRIPTS / "omniroute-worker.ps1"

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

# Driver: dot-source the helper, resolve, and print either the path or the error.
DRIVER = r"""
param([string]$LocalAppData)
$ErrorActionPreference = "Stop"
. "{helper}"
try {{
    $path = Resolve-CodexExecutable -LocalAppData $LocalAppData
    Write-Output ("RESOLVED:" + $path)
    exit 0
}} catch {{
    Write-Output ("ERROR:" + $_.Exception.Message)
    exit 3
}}
"""


def _clean_env(local_app_data: str) -> dict[str, str]:
    """Environment with no Codex on PATH, so resolution must not depend on it."""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if "codex" not in entry.lower() and "openai" not in entry.lower()
    )
    env["LOCALAPPDATA"] = local_app_data
    return env


@unittest.skipUnless(POWERSHELL, "PowerShell is not available on this host")
class CodexExecutableResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nexus_codex_resolve_")
        self.local_app_data = self._tmp.name
        self.bin_root = Path(self.local_app_data) / "OpenAI" / "Codex" / "bin"
        self.driver = Path(self._tmp.name) / "driver.ps1"
        self.driver.write_text(
            DRIVER.format(helper=str(HELPER)), encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _install_codex(self, folder: str, mtime: float | None = None) -> Path:
        exe = self.bin_root / folder / "codex.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ-test-stub")
        if mtime is not None:
            os.utime(exe, (mtime, mtime))
        return exe

    def _resolve(self) -> tuple[int, str]:
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.driver),
                "-LocalAppData",
                self.local_app_data,
            ],
            env=_clean_env(self.local_app_data),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return completed.returncode, completed.stdout + completed.stderr

    # CASE A: Codex is not on PATH but exists in the installation directory.
    def test_resolves_codex_from_installation_directory_without_path(self):
        exe = self._install_codex("8aaf1547b825b104")

        code, output = self._resolve()

        self.assertEqual(code, 0, output)
        resolved = re.search(r"RESOLVED:(.+)", output)
        self.assertIsNotNone(resolved, output)
        self.assertEqual(
            Path(resolved.group(1).strip()).resolve(), exe.resolve()
        )

    # CASE B: several versioned folders; the newest codex.exe wins.
    def test_selects_newest_candidate_by_modification_time(self):
        old = self._install_codex("aaaa1111", mtime=1_700_000_000)
        new = self._install_codex("bbbb2222", mtime=1_800_000_000)
        self._install_codex("cccc3333", mtime=1_600_000_000)

        code, output = self._resolve()

        self.assertEqual(code, 0, output)
        resolved = re.search(r"RESOLVED:(.+)", output).group(1).strip()
        self.assertEqual(Path(resolved).resolve(), new.resolve())
        self.assertNotEqual(Path(resolved).resolve(), old.resolve())

    # CASE C: nothing installed -> fail fast with a clear, credential-free error.
    def test_fails_fast_when_codex_is_absent_from_supported_location(self):
        self.bin_root.mkdir(parents=True)

        code, output = self._resolve()

        self.assertEqual(code, 3, output)
        self.assertIn("CODEX_EXECUTABLE_NOT_FOUND", output)
        self.assertIn(str(self.bin_root), output)
        self.assertNotIn("RESOLVED:", output)

    def test_fails_when_versioned_folder_has_no_codex_exe(self):
        (self.bin_root / "f1e5e36960c35938").mkdir(parents=True)
        (self.bin_root / "f1e5e36960c35938" / "rg.exe").write_bytes(b"MZ")

        code, output = self._resolve()

        self.assertEqual(code, 3, output)
        self.assertIn("CODEX_EXECUTABLE_NOT_FOUND", output)

    def test_directory_named_codex_exe_is_not_selected(self):
        (self.bin_root / "dddd4444" / "codex.exe").mkdir(parents=True)

        code, output = self._resolve()

        self.assertEqual(code, 3, output)
        self.assertIn("CODEX_EXECUTABLE_NOT_FOUND", output)

    def test_fails_when_localappdata_is_unset(self):
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.driver),
                "-LocalAppData",
                "",
            ],
            env=_clean_env(self.local_app_data),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
        self.assertIn("CODEX_EXECUTABLE_NOT_FOUND", completed.stdout)


class WorkerWrapperSafetyTests(unittest.TestCase):
    """CASE D: the change must not weaken or alter the generated Worker safety profile."""

    @classmethod
    def setUpClass(cls):
        cls.wrapper_text = WRAPPER.read_text(encoding="utf-8")
        cls.helper_text = HELPER.read_text(encoding="utf-8")

    def test_wrapper_no_longer_depends_on_path_lookup(self):
        self.assertNotRegex(self.wrapper_text, r"Get-Command\s+codex")
        self.assertIn('Join-Path $PSScriptRoot "codex-resolution.ps1"', self.wrapper_text)
        self.assertIn("Resolve-CodexExecutable", self.wrapper_text)

    def test_wrapper_invokes_resolved_absolute_executable(self):
        self.assertIn("$Task | & $codexExecutable @codexArguments", self.wrapper_text)

    def test_wrapper_safety_profile_checks_unchanged(self):
        self.assertIn(
            r"""'(?m)^sandbox_mode\s*=\s*"workspace-write"\s*$'""",
            self.wrapper_text,
        )
        self.assertIn("danger-full-access", self.wrapper_text)
        self.assertIn('--sandbox", $Sandbox', self.wrapper_text)
        self.assertIn("approval_policy=\"never\"", self.wrapper_text)

    def test_helper_does_not_touch_path_or_safety_config(self):
        self.assertNotIn("danger-full-access", self.helper_text)
        self.assertNotRegex(self.helper_text, r"Environment\]::SetEnvironmentVariable")
        self.assertNotRegex(self.helper_text, r"\$env:PATH\s*=")
        self.assertNotIn("sandbox_mode", self.helper_text)
