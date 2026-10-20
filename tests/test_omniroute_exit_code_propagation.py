"""
tests/test_omniroute_exit_code_propagation.py

Validates exit-code integrity and the Phase 4.1H-C native worker result
channel for the OmniRoute dispatcher.

Section 2: WrapperExitCodePropagationTests
    Proves the minimal PowerShell propagation pattern:
      inner executable exits 0  → wrapper exits 0
      inner executable exits N  → wrapper exits N
    Tests are skipped when powershell.exe is not available (Linux CI).

Section 3: Covered by WrapperExitCodePropagationTests
    (test_inner_exit_7_wrapper_exits_7 — NONZERO_TEST_CODE = 7)

Section 4: RunOmniRouteWorkerExitCodeTests
    run_omniroute_worker() uses process.wait() only (no stdout parsing):
      wrapper returncode N≠0 → status "FAILED"
      wrapper returncode 0 + result file present + non-empty → "COMPLETED"

Section 4b: NativeResultChannelTests (Phase 4.1H-C)
    Proves the --output-last-message contract:
      exit 0 + file missing   → FAILED / WORKER_RESULT_MISSING
      exit 0 + file empty     → FAILED / WORKER_RESULT_EMPTY
      exit 0 + file ws-only   → FAILED / WORKER_RESULT_EMPTY
      exit 0 + file "OK"      → COMPLETED / output == "OK"
      exit 0 + file "1"       → COMPLETED
      exit 0 + file "80000"   → COMPLETED
      exit 0 + file "sink_nodes" → COMPLETED
      exit 0 + multiline file → COMPLETED / exact content preserved
      diagnostic noise + valid file → COMPLETED / output clean (no noise)
      diagnostic non-empty + file missing + exit 0 → FAILED (4.1H-A gap closed)
      exit 1 + file present   → FAILED (nonzero exit authoritative)
      concurrent executions   → unique result paths (no collision)
      temp file cleanup       → file removed in all outcomes
      FAILED agents.status persisted correctly
      COMPLETED agents.result = semantic content, not diagnostic stream

Section 5: ProgressiveLifecycleFailedWorkerTests
    execute_progressively() with a FAILED worker:
      - Returns {"status": "FAILED", "reason": "WORKER_EXECUTION_FAILED"}
      - review_worker() is NOT called
      - Result status is not "COMPLETED"
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.runtime_assets import OMNIROUTE_WORKER_SCRIPT


# ─────────────────────────────────────────────────────────────────────────────
# Helper: PowerShell availability detection
# ─────────────────────────────────────────────────────────────────────────────

def _powershell_available() -> bool:
    """Return True when powershell.exe is reachable on PATH."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "exit 0"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def test_repository_wrapper_contract_is_structured_and_exit_code_is_immediate():
    source = OMNIROUTE_WORKER_SCRIPT.read_text(encoding="utf-8")

    for parameter in ("$Repo", "$Model", "$Effort", "$Task", "$ResultFile", "$Sandbox"):
        assert parameter in source
    assert '"--output-last-message", $ResultFile' in source
    assert '"--sandbox", $Sandbox' in source
    assert '"--cd", $Repo' in source
    assert 'A workspace-write Worker requires an assigned linked worktree' in source
    assert "windows.sandbox" not in source.lower()
    assert "approval_policy=\"never\"" in source
    assert "$Task | & $codexExecutable @codexArguments" in source
    assert "$workerExitCode = $LASTEXITCODE\nexit $workerExitCode" in source
    assert "cmd /c" not in source.lower()
    assert "rmdir /s /q" not in source.lower()


@unittest.skipUnless(_powershell_available(), "powershell.exe not available")
def test_repository_wrapper_parses_as_powershell_without_execution():
    command = (
        "$errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{str(OMNIROUTE_WORKER_SCRIPT).replace("'", "''")}', "
        "[ref]$null, [ref]$errors) > $null; "
        "if ($errors.Count -ne 0) { exit 1 }; exit 0"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


_PS_AVAILABLE = _powershell_available()


def _run_ps_script(script_text: str, extra_args: list | None = None) -> int:
    """Write script_text to a temp .ps1 file, invoke it, return exit code only."""
    with tempfile.NamedTemporaryFile(
        suffix=".ps1", mode="w", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script_text)
        tmp = fh.name
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                tmp,
            ]
            + (extra_args or []),
            capture_output=True,
            timeout=30,
        )
        return result.returncode
    finally:
        Path(tmp).unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sections 2 & 3 — Wrapper exit-code propagation (PowerShell layer)
# ─────────────────────────────────────────────────────────────────────────────

@unittest.skipUnless(_PS_AVAILABLE, "powershell.exe not available in this environment")
class WrapperExitCodePropagationTests(unittest.TestCase):
    """
    Minimal PowerShell boundary test.  We use a small synthetic script that
    exercises the exact pattern added to omniroute-worker.ps1:

        & <inner>; $workerExitCode = $LASTEXITCODE; ...; exit $workerExitCode

    `cmd /c exit <N>` stands in for the inner Codex process so no external
    infrastructure (git, codex.exe, CODEX_HOME, …) is required.

    Exit codes are validated via process.returncode — stdout is NOT parsed.
    """

    # Mirrors the capture-and-propagate pattern in omniroute-worker.ps1.
    _PROPAGATION_SCRIPT = textwrap.dedent(r"""
        param([int]$ExitWith)

        # Simulate inner process with the requested exit code
        cmd /c exit $ExitWith

        # Capture immediately — mirrors omniroute-worker.ps1 line 101
        $workerExitCode = $LASTEXITCODE

        # Subsequent commands that update $LASTEXITCODE (mirrors git status / diff)
        Write-Host "Exit code : $workerExitCode"
        Write-Host "Model     : synthetic"
        Write-Host "Branch    : test/branch"
        Write-Host "WORKTREE_PRESERVED=C:\fake\worktree"

        # Final propagation — the fix added to omniroute-worker.ps1
        exit $workerExitCode
    """).strip()

    def _run_with_code(self, code: int) -> int:
        return _run_ps_script(self._PROPAGATION_SCRIPT, extra_args=[str(code)])

    def test_inner_exit_0_wrapper_exits_0(self):
        """Section 2: inner executable exits 0 → wrapper exits 0."""
        actual = self._run_with_code(0)
        self.assertEqual(actual, 0)

    def test_inner_exit_7_wrapper_exits_7(self):
        """Section 3: inner executable exits 7 → wrapper exits 7 (NONZERO_TEST_CODE=7)."""
        actual = self._run_with_code(7)
        self.assertEqual(actual, 7)

    def test_inner_exit_1_wrapper_exits_1(self):
        """
        RUN-B28DF4FB scenario: inner Codex exits 1 (403 Forbidden) →
        wrapper MUST exit 1.  Before the fix the wrapper exited 0.
        """
        actual = self._run_with_code(1)
        self.assertEqual(actual, 1)

    def test_subsequent_commands_do_not_pollute_exit_code(self):
        """
        $workerExitCode is captured immediately; Write-Host / git-style
        commands after the capture must NOT alter the propagated exit code.
        Inner exits 3; Write-Host exits 0; wrapper must still exit 3.
        """
        script = textwrap.dedent(r"""
            cmd /c exit 3
            $workerExitCode = $LASTEXITCODE

            # These lines update $LASTEXITCODE to 0 via Write-Host (external var is int)
            Write-Host "This line succeeds (LASTEXITCODE → 0)"
            Write-Host "Exit code : $workerExitCode"

            exit $workerExitCode
        """).strip()
        actual = _run_ps_script(script)
        self.assertEqual(actual, 3)


# ─────────────────────────────────────────────────────────────────────────────
# DB fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

_FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        }
    ]
}


class _DbMixin(unittest.TestCase):
    """Sets up a fresh in-process SQLite database for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp.name)
        # These tests mock the worker process itself; creating real Codex
        # profiles under the user's LOCALAPPDATA is unrelated to the contract
        # under test and breaks isolation on restricted Windows runners.
        self._codex_env_patcher = patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            return_value={},
        )
        self._codex_env_patcher.start()

        self._orig_db = database.DATABASE_PATH
        self._orig_storage = database.STORAGE_DIR
        self._orig_projects = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "test.db"

        fixture = tmp_path / "projects.json"
        fixture.write_text(json.dumps(_FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture

        database.initialize_database()
        projects.sync_projects()

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO runs (id,project_id,input,intent,status,created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("RUN-PROP-TEST", "norte", "do work", "GO", "RUNNING", "now"),
            )

    def tearDown(self):
        self._codex_env_patcher.stop()
        database.DATABASE_PATH = self._orig_db
        database.STORAGE_DIR = self._orig_storage
        projects.PROJECTS_FILE = self._orig_projects
        self._tmp.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# Popen side-effect factory for result-file-aware mocks
# ─────────────────────────────────────────────────────────────────────────────

def _make_popen_side_effect(
    returncode: int,
    file_content: str | None,  # None = don't write file (simulate MISSING)
    stdout_lines: list[str] | None = None,
) -> "callable":
    """
    Returns a side-effect function for patching subprocess.Popen.

    When called, it:
    1. Locates the -ResultFile argument in the command list.
    2. If file_content is not None, writes it to that path (simulating
       what `codex exec --output-last-message <path>` does on success).
    3. Returns a mock process with the given returncode and stdout_lines.
    """
    def _side_effect(command, **kwargs):
        # Find the -ResultFile argument in the command list
        result_file_path: str | None = None
        try:
            idx = command.index("-ResultFile")
            result_file_path = command[idx + 1]
        except (ValueError, IndexError):
            pass

        if result_file_path is not None and file_content is not None:
            Path(result_file_path).write_text(file_content, encoding="utf-8")

        proc = MagicMock()
        proc.stdout = iter(stdout_lines or [])
        proc.wait.return_value = returncode
        return proc

    return _side_effect


# ─────────────────────────────────────────────────────────────────────────────
# Section 4 — Python dispatcher: wrapper returncode → status mapping
# ─────────────────────────────────────────────────────────────────────────────

class RunOmniRouteWorkerExitCodeTests(_DbMixin):
    """
    Verifies that run_omniroute_worker() maps the wrapper process returncode
    to worker status using process.wait() — no stdout parsing.

    Phase 4.1H-C: COMPLETED now additionally requires a non-empty result file
    written by codex via --output-last-message.
    """

    def _call_dispatcher(
        self,
        returncode: int,
        file_content: str | None = None,
        stdout_lines: list | None = None,
    ) -> dict:
        from nexus.dispatchers.omniroute import run_omniroute_worker

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_make_popen_side_effect(
                    returncode=returncode,
                    file_content=file_content,
                    stdout_lines=stdout_lines,
                ),
            ),
        ):
            mock_path.exists.return_value = True
            return run_omniroute_worker(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                task="do work",
                model="oc/big-pickle",
            )

    # ── success path ──────────────────────────────────────────────────────────

    def test_wrapper_returncode_0_maps_to_completed(self):
        """Section 4: wrapper returncode 0 + result file → status COMPLETED."""
        result = self._call_dispatcher(
            returncode=0,
            file_content="Worker completed the task successfully.",
            stdout_lines=[
                "Branch    : worker/abc\n",
                "WORKTREE_PRESERVED=C:\\wt\n",
            ],
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)

    def test_worktree_defaults_to_assigned_repo_when_wrapper_reports_none(self):
        """The real omniroute-worker.ps1 runs codex in the assigned linked
        worktree and prints no WORKTREE metadata; review needs that path."""
        result = self._call_dispatcher(
            returncode=0,
            file_content="Worker completed the task successfully.",
            stdout_lines=["Model : cc/claude-sonnet-5-low\n"],
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["worktree"], r"C:\fake\interface-life")

    # ── failure paths ─────────────────────────────────────────────────────────

    def test_wrapper_returncode_1_maps_to_failed(self):
        """Section 4: wrapper returncode 1 → status FAILED (RUN-B28DF4FB scenario)."""
        result = self._call_dispatcher(
            returncode=1,
            file_content=None,
            stdout_lines=["Exit code : 1\n"],
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 1)

    def test_wrapper_returncode_7_maps_to_failed(self):
        """Section 4: wrapper returncode 7 → status FAILED (NONZERO_TEST_CODE=7)."""
        result = self._call_dispatcher(returncode=7, file_content=None)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 7)

    # ── no stdout parsing proof ───────────────────────────────────────────────

    def test_no_stdout_parsing_returncode_beats_stdout_claim(self):
        """
        CRITICAL anti-test: stdout claims success ("Exit code : 0") but the
        actual process returncode is 1.  Result MUST be FAILED — proving that
        Python trusts process.wait() exclusively and does NOT parse stdout.
        """
        result = self._call_dispatcher(
            returncode=1,
            file_content=None,
            stdout_lines=["Exit code : 0\n"],  # Lying stdout — must be ignored
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 1)

    def test_no_stdout_parsing_success_confirmed_by_returncode_only(self):
        """
        Phase 4.1H-C: returncode 0 with no result file → FAILED(WORKER_RESULT_MISSING).
        A zero exit code is necessary but not sufficient; the result file
        (written by codex via --output-last-message) must also exist and be
        non-empty.  This closes the Phase 4.1H-A gap where diagnostic-only
        stdout could produce a false COMPLETED.
        """
        # file_content=None means Popen side-effect does NOT write the result file
        result = self._call_dispatcher(returncode=0, file_content=None, stdout_lines=[])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["reason"], "WORKER_RESULT_MISSING")


# ─────────────────────────────────────────────────────────────────────────────
# Section 4b — Phase 4.1H-C: native result channel (--output-last-message)
# ─────────────────────────────────────────────────────────────────────────────

class NativeResultChannelTests(_DbMixin):
    """
    Phase 4.1H-C: proves the --output-last-message contract for
    run_omniroute_worker().

    The semantic result is the LAST-MESSAGE FILE content, not stdout.
    Transport logs / reconnect spam / wrapper lines remain in the diagnostic
    stream only.

    Contract:
      exit_code != 0                   → FAILED (existing, unchanged)
      exit_code == 0, file missing     → FAILED / WORKER_RESULT_MISSING
      exit_code == 0, file empty/ws    → FAILED / WORKER_RESULT_EMPTY
      exit_code == 0, file non-empty   → COMPLETED / output = file content
    """

    def _call(
        self,
        returncode: int,
        file_content: str | None,
        stdout_lines: list[str] | None = None,
    ) -> dict:
        from nexus.dispatchers.omniroute import run_omniroute_worker

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_make_popen_side_effect(
                    returncode=returncode,
                    file_content=file_content,
                    stdout_lines=stdout_lines,
                ),
            ),
        ):
            mock_path.exists.return_value = True
            return run_omniroute_worker(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                task="do work",
                model="oc/big-pickle",
            )

    # ── Test A: result file missing → WORKER_RESULT_MISSING ──────────────────

    def test_exit0_result_file_missing_is_failed_with_missing_reason(self):
        """A: exit 0 + result file missing → FAILED / WORKER_RESULT_MISSING."""
        result = self._call(returncode=0, file_content=None)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["reason"], "WORKER_RESULT_MISSING")
        self.assertIsNone(result["output"])

    def test_exit0_result_file_missing_stdout_non_empty_still_failed(self):
        """
        J (Phase 4.1H-A gap): diagnostic stdout has content but result file
        is missing → FAILED / WORKER_RESULT_MISSING.
        Proves that non-empty diagnostic stream cannot mask a missing result.
        """
        result = self._call(
            returncode=0,
            file_content=None,
            stdout_lines=[
                "Reconnecting to OmniRoute...\n",
                "Branch    : worker/abc\n",
                "WORKTREE_PRESERVED=C:\\wt\n",
                "Exit code : 0\n",
            ],
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reason"], "WORKER_RESULT_MISSING")

    # ── Test B: result file empty → WORKER_RESULT_EMPTY ──────────────────────

    def test_exit0_result_file_empty_is_failed_with_empty_reason(self):
        """B: exit 0 + result file exists but empty → FAILED / WORKER_RESULT_EMPTY."""
        result = self._call(returncode=0, file_content="")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["reason"], "WORKER_RESULT_EMPTY")
        self.assertIsNone(result["output"])

    # ── Test C: result file whitespace-only → WORKER_RESULT_EMPTY ────────────

    def test_exit0_result_file_whitespace_only_is_failed(self):
        """C: exit 0 + result file whitespace-only → FAILED / WORKER_RESULT_EMPTY."""
        result = self._call(returncode=0, file_content="   \n\t\n  ")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reason"], "WORKER_RESULT_EMPTY")

    # ── Test D: "OK" → COMPLETED, output=="OK" ────────────────────────────────

    def test_exit0_result_ok_is_completed(self):
        """D: exit 0 + result file 'OK' → COMPLETED / output=='OK'."""
        result = self._call(returncode=0, file_content="OK")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "OK")
        self.assertNotIn("reason", result)

    # ── Test E: "1" → COMPLETED ───────────────────────────────────────────────

    def test_exit0_result_single_number_is_completed(self):
        """E: exit 0 + result file '1' → COMPLETED (single char, no minimum length)."""
        result = self._call(returncode=0, file_content="1")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], "1")

    # ── Test F: "80000" → COMPLETED ──────────────────────────────────────────

    def test_exit0_result_80000_is_completed(self):
        """F: exit 0 + result file '80000' → COMPLETED (Phase 3 token)."""
        result = self._call(returncode=0, file_content="80000")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], "80000")

    # ── Test G: "sink_nodes" → COMPLETED ─────────────────────────────────────

    def test_exit0_result_sink_nodes_is_completed(self):
        """G: exit 0 + result file 'sink_nodes' → COMPLETED (Phase 3 token)."""
        result = self._call(returncode=0, file_content="sink_nodes")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], "sink_nodes")

    # ── Test H: multiline → COMPLETED, exact content preserved ───────────────

    def test_exit0_result_multiline_exact_content_preserved(self):
        """H: exit 0 + multiline result → COMPLETED / exact semantic content preserved."""
        semantic = "Files changed: foo.py, bar.py\nValidation: tests passed\nResult: OK"
        result = self._call(
            returncode=0,
            file_content=semantic,
            stdout_lines=[
                "Branch    : worker/abc\n",
                "WORKTREE_PRESERVED=C:\\wt\n",
                "codex metadata: connecting...\n",
                "[codex] tokens: 123\n",
            ],
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], semantic)

    # ── Test I: diagnostic noise + valid result file → clean output ──────────

    def test_diagnostic_noise_does_not_pollute_worker_output(self):
        """
        I: diagnostic stdout contains reconnect/noise, result file is 'OK'.
        COMPLETED; authoritative output == 'OK'; noise NOT propagated.
        """
        result = self._call(
            returncode=0,
            file_content="OK",
            stdout_lines=[
                "Reconnecting to OmniRoute...\n",
                "ERROR: connection reset, retrying\n",
                "WARNING: reconnect attempt 3/5\n",
                "Branch    : worker/abc\n",
                "WORKTREE_PRESERVED=C:\\wt\n",
            ],
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], "OK")
        # Reconnect noise must NOT be in the authoritative output
        self.assertNotIn("Reconnecting", result["output"])
        self.assertNotIn("ERROR", result["output"])

    # ── Test K: exit != 0 + result file present → FAILED ────────────────────

    def test_nonzero_exit_with_result_file_is_failed(self):
        """K: exit_code != 0 + result file non-empty → FAILED (exit code authoritative)."""
        result = self._call(
            returncode=1,
            file_content="Some output that should be ignored",
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 1)
        # reason should NOT be WORKER_RESULT_MISSING or WORKER_RESULT_EMPTY
        self.assertNotIn(result.get("reason", ""), ["WORKER_RESULT_MISSING", "WORKER_RESULT_EMPTY"])

    def test_nonzero_exit_7_with_result_file_is_failed(self):
        """exit_code=7 + result file present → FAILED."""
        result = self._call(returncode=7, file_content="non-empty content")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 7)

    # ── COMPLETED result carries no reason key ────────────────────────────────

    def test_completed_result_has_no_reason_key(self):
        """A successful run must NOT include a 'reason' key at all."""
        result = self._call(
            returncode=0,
            file_content="Worker finished successfully.",
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("reason", result)

    # ── Test N: concurrent executions → unique result paths ──────────────────

    def test_concurrent_executions_have_unique_result_paths(self):
        """
        N: under concurrent execution, each call gets a distinct result file path.
        No two workers share a path (concurrency-safe via mkstemp).
        """
        from nexus.dispatchers.omniroute import run_omniroute_worker

        collected_paths: list[str] = []
        lock = threading.Lock()

        def _recording_popen(command, **kwargs):
            try:
                idx = command.index("-ResultFile")
                path = command[idx + 1]
                with lock:
                    collected_paths.append(path)
                # Write content so the run can complete as COMPLETED
                Path(path).write_text("concurrent-ok", encoding="utf-8")
            except (ValueError, IndexError):
                pass
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = 0
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.codex_subprocess_env",
                return_value={},
            ),
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_recording_popen,
            ),
        ):
            mock_path.exists.return_value = True

            threads = []
            for _ in range(5):
                t = threading.Thread(
                    target=lambda: run_omniroute_worker(
                        run_id="RUN-PROP-TEST",
                        repo=r"C:\fake\interface-life",
                        task="do work",
                        model="oc/big-pickle",
                    )
                )
                threads.append(t)
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(collected_paths), 5)
        # All paths must be unique
        self.assertEqual(len(set(collected_paths)), 5, "Each worker must have a unique result file path")

    # ── Test O: temp file cleanup ─────────────────────────────────────────────

    def test_result_file_cleaned_up_after_completed_run(self):
        """O: temp result file is removed after a successful (COMPLETED) run."""
        recorded_path: list[str] = []

        def _recording_popen(command, **kwargs):
            try:
                idx = command.index("-ResultFile")
                path = command[idx + 1]
                recorded_path.append(path)
                Path(path).write_text("cleanup-test-content", encoding="utf-8")
            except (ValueError, IndexError):
                pass
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = 0
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_recording_popen,
            ),
        ):
            mock_path.exists.return_value = True
            from nexus.dispatchers.omniroute import run_omniroute_worker
            result = run_omniroute_worker(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                task="do work",
                model="oc/big-pickle",
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(len(recorded_path) == 1)
        # File must have been cleaned up
        self.assertFalse(
            Path(recorded_path[0]).exists(),
            "Result file must be removed after use",
        )

    def test_result_file_cleaned_up_after_failed_run(self):
        """O: temp result file is removed even when the run fails (WORKER_RESULT_MISSING)."""
        recorded_path: list[str] = []

        def _recording_popen(command, **kwargs):
            try:
                idx = command.index("-ResultFile")
                path = command[idx + 1]
                recorded_path.append(path)
                # Deliberately do NOT write the result file → WORKER_RESULT_MISSING
            except (ValueError, IndexError):
                pass
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = 0
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_recording_popen,
            ),
        ):
            mock_path.exists.return_value = True
            from nexus.dispatchers.omniroute import run_omniroute_worker
            result = run_omniroute_worker(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                task="do work",
                model="oc/big-pickle",
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reason"], "WORKER_RESULT_MISSING")
        if recorded_path:
            # If a path was recorded, the file should not exist (either never written or cleaned up)
            self.assertFalse(Path(recorded_path[0]).exists())

    # ── Test P: persisted agents.status is FAILED for missing/empty result ────

    def test_exit0_missing_result_persisted_agent_status_is_failed(self):
        """P: exit_code=0 + file missing → agents table row status='FAILED'."""
        result = self._call(returncode=0, file_content=None)

        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM agents WHERE id = ?",
                (result["agent_id"],),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "FAILED")

    def test_exit0_empty_result_persisted_agent_status_is_failed(self):
        """P: exit_code=0 + file empty → agents table row status='FAILED'."""
        result = self._call(returncode=0, file_content="")

        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM agents WHERE id = ?",
                (result["agent_id"],),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "FAILED")

    # ── Test Q: persisted semantic output, not diagnostic stream ──────────────

    def test_completed_agents_result_is_semantic_content_not_diagnostic(self):
        """
        Q: COMPLETED run → agents.result column = semantic last-message content,
        NOT the merged diagnostic transcript.
        """
        semantic = "Task completed: modified foo.py"
        result = self._call(
            returncode=0,
            file_content=semantic,
            stdout_lines=[
                "OmniRoute transport log: connecting...\n",
                "Branch    : worker/abc\n",
                "[provider] tokens: 999\n",
            ],
        )

        self.assertEqual(result["status"], "COMPLETED")

        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT result FROM agents WHERE id = ?",
                (result["agent_id"],),
            ).fetchone()

        self.assertIsNotNone(row)
        stored = row["result"]
        self.assertIsNotNone(stored)
        # Must contain the semantic content
        self.assertIn("Task completed", stored)
        # Must NOT be the raw transport log
        self.assertNotIn("OmniRoute transport log", stored)
        self.assertNotIn("connecting", stored)

    def test_failed_agent_result_column_is_not_semantic_content(self):
        """
        Q counterpart: FAILED run (missing file) → agents.result is None
        or diagnostic, never a spurious 'semantic' value.
        """
        result = self._call(
            returncode=0,
            file_content=None,
            stdout_lines=["diagnostic noise\n"],
        )
        self.assertEqual(result["status"], "FAILED")
        # output key must be None for FAILED
        self.assertIsNone(result["output"])


# ─────────────────────────────────────────────────────────────────────────────
# Section 5 — Progressive lifecycle: FAILED worker → review never called
# ─────────────────────────────────────────────────────────────────────────────

class ProgressiveLifecycleFailedWorkerTests(_DbMixin):
    """
    Proves the execute_progressively() early-return gate:

      worker_result["status"] != "COMPLETED"
        → return {"status": "FAILED", "reason": "WORKER_EXECUTION_FAILED"}
        → review_worker() is NOT called
        → result status is NOT "COMPLETED"

    The architecture of progressive.py explicitly returns early before the
    review_worker() call when the worker did not complete.  This test proves
    that gate is intact and that a fixed wrapper (which now propagates exit
    code 1 → wrapper exit 1 → status FAILED) cannot reach the review stage.
    """

    _PLANNED_WORKER = {
        "route_class": "mechanical",
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "oc/big-pickle",
        "effort": "low",
        "scope": "Fix the thing.",
        "reason": "Simple mechanical change.",
        "depends_on": [],
    }

    def _make_routing_service(self):
        from nexus.routing.service import RoutingDecision

        svc = MagicMock()
        svc.select_route_for_capability.return_value = RoutingDecision(
            model="oc/big-pickle",
            provider="omniroute",
            effort="low",
            execution_path="OMNIROUTE",
            reason="test",
            degraded=False,
        )
        return svc

    def test_failed_worker_returns_failed_and_does_not_call_review(self):
        """
        Section 5: when execute_worker() returns status FAILED,
        execute_progressively() must return failure immediately and must
        NOT call review_worker().
        """
        from nexus.orchestration.progressive import execute_progressively

        failed_worker_result = {
            "agent_id": "AGT-FAKE",
            "status": "FAILED",
            "exit_code": 1,
            "branch": None,
            "worktree": None,
            "output": None,
        }

        with (
            patch(
                "nexus.orchestration.progressive.execute_worker",
                return_value=failed_worker_result,
            ),
            patch(
                "nexus.orchestration.progressive.review_worker",
            ) as mock_review,
            patch("nexus.orchestration.progressive.record_checkpoint"),
            patch("nexus.orchestration.progressive.update_run_status"),
        ):
            result = execute_progressively(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                manager_id="MGR-FAKE",
                original_task="do work",
                planned_worker=self._PLANNED_WORKER,
                plan_risk="LOW",
                routing_service=self._make_routing_service(),
                worker_ordinal=1,
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reason"], "WORKER_EXECUTION_FAILED")
        mock_review.assert_not_called()
        self.assertNotEqual(result["status"], "COMPLETED")

    def test_failed_worker_result_is_carried_in_return(self):
        """
        The worker result dict is propagated in the return value so callers
        can inspect the failure (exit code, agent_id, etc.) without querying DB.
        """
        from nexus.orchestration.progressive import execute_progressively

        failed_worker_result = {
            "agent_id": "AGT-FAILED-123",
            "status": "FAILED",
            "exit_code": 7,
            "branch": None,
            "worktree": None,
            "output": None,
        }

        with (
            patch(
                "nexus.orchestration.progressive.execute_worker",
                return_value=failed_worker_result,
            ),
            patch("nexus.orchestration.progressive.review_worker") as mock_review,
            patch("nexus.orchestration.progressive.record_checkpoint"),
            patch("nexus.orchestration.progressive.update_run_status"),
        ):
            result = execute_progressively(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                manager_id="MGR-FAKE",
                original_task="do work",
                planned_worker=self._PLANNED_WORKER,
                plan_risk="LOW",
                routing_service=self._make_routing_service(),
                worker_ordinal=1,
            )

        self.assertEqual(result["worker"]["agent_id"], "AGT-FAILED-123")
        self.assertEqual(result["worker"]["exit_code"], 7)
        mock_review.assert_not_called()

    def test_launch_failed_worker_also_skips_review(self):
        """
        LAUNCH_FAILED (e.g. ADAPTER_PATH missing) is also non-COMPLETED;
        review must not be called for this path either.
        """
        from nexus.orchestration.progressive import execute_progressively

        launch_failed_result = {
            "agent_id": "AGT-LF",
            "status": "LAUNCH_FAILED",
            "exit_code": None,
            "branch": None,
            "worktree": None,
            "error": "OmniRoute Worker adapter not found",
        }

        with (
            patch(
                "nexus.orchestration.progressive.execute_worker",
                return_value=launch_failed_result,
            ),
            patch("nexus.orchestration.progressive.review_worker") as mock_review,
            patch("nexus.orchestration.progressive.record_checkpoint"),
            patch("nexus.orchestration.progressive.update_run_status"),
        ):
            result = execute_progressively(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                manager_id="MGR-FAKE",
                original_task="do work",
                planned_worker=self._PLANNED_WORKER,
                plan_risk="LOW",
                routing_service=self._make_routing_service(),
                worker_ordinal=1,
            )

        self.assertEqual(result["status"], "FAILED")
        mock_review.assert_not_called()

    # ── L + M: REVIEW_NOT_CALLED + NO_NEW_RETRY for WORKER_RESULT_MISSING ────

    def test_worker_result_missing_does_not_call_review_and_no_retry(self):
        """
        L + M: Phase 4.1H-C REVIEW_NOT_CALLED + NO_NEW_RETRY.

        When run_omniroute_worker returns status=FAILED / reason=WORKER_RESULT_MISSING
        (exit_code=0 + result file absent), execute_progressively must:

          1. Short-circuit before review_worker — review_worker NOT called.
          2. Return {"status": "FAILED", "reason": "WORKER_EXECUTION_FAILED"}.
          3. Call execute_worker exactly once — no retry ladder, no escalation.
        """
        from nexus.orchestration.progressive import execute_progressively

        worker_result_missing = {
            "agent_id": "AGT-MISSING",
            "status": "FAILED",
            "reason": "WORKER_RESULT_MISSING",
            "exit_code": 0,
            "branch": None,
            "worktree": None,
            "output": None,
        }

        with (
            patch(
                "nexus.orchestration.progressive.execute_worker",
                return_value=worker_result_missing,
            ) as mock_execute,
            patch(
                "nexus.orchestration.progressive.review_worker",
            ) as mock_review,
            patch("nexus.orchestration.progressive.record_checkpoint"),
            patch("nexus.orchestration.progressive.update_run_status"),
        ):
            result = execute_progressively(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                manager_id="MGR-FAKE",
                original_task="do work",
                planned_worker=self._PLANNED_WORKER,
                plan_risk="LOW",
                routing_service=self._make_routing_service(),
                worker_ordinal=1,
            )

        # L: REVIEW_NOT_CALLED
        mock_review.assert_not_called()
        # M: NO_NEW_RETRY
        mock_execute.assert_called_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reason"], "WORKER_EXECUTION_FAILED")
        self.assertEqual(result["worker"]["reason"], "WORKER_RESULT_MISSING")
        self.assertEqual(result["worker"]["exit_code"], 0)

    def test_worker_result_empty_does_not_call_review_and_no_retry(self):
        """
        L + M: WORKER_RESULT_EMPTY follows the same gate as WORKER_RESULT_MISSING.
        review_worker NOT called; execute_worker called exactly once.
        """
        from nexus.orchestration.progressive import execute_progressively

        worker_result_empty = {
            "agent_id": "AGT-EMPTY",
            "status": "FAILED",
            "reason": "WORKER_RESULT_EMPTY",
            "exit_code": 0,
            "branch": None,
            "worktree": None,
            "output": None,
        }

        with (
            patch(
                "nexus.orchestration.progressive.execute_worker",
                return_value=worker_result_empty,
            ) as mock_execute,
            patch(
                "nexus.orchestration.progressive.review_worker",
            ) as mock_review,
            patch("nexus.orchestration.progressive.record_checkpoint"),
            patch("nexus.orchestration.progressive.update_run_status"),
        ):
            result = execute_progressively(
                run_id="RUN-PROP-TEST",
                repo=r"C:\fake\interface-life",
                manager_id="MGR-FAKE",
                original_task="do work",
                planned_worker=self._PLANNED_WORKER,
                plan_risk="LOW",
                routing_service=self._make_routing_service(),
                worker_ordinal=1,
            )

        mock_review.assert_not_called()
        mock_execute.assert_called_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["worker"]["reason"], "WORKER_RESULT_EMPTY")


if __name__ == "__main__":
    unittest.main()
