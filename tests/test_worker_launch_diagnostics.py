"""
tests/test_worker_launch_diagnostics.py

Observability for Worker launch failures. These tests cover what is persisted
when a Worker fails before its process starts, not any change in execution
semantics.

Contract under test:
  - the assigned worktree is persisted before launch preparation;
  - a launch failure records its phase, exception type and a bounded, redacted
    message on the agent row and in the WORKER_ATTEMPT checkpoint fields;
  - a successful Worker gets no launch diagnostic;
  - serialized diagnostics never contain environment or credential material.
"""

import os
import unittest
from unittest.mock import patch

import nexus.registry.database as database
from nexus.dispatchers import omniroute
from nexus.dispatchers.omniroute import (
    LAUNCH_FAILED_REASON,
    _safe_launch_message,
    run_omniroute_worker,
)
from nexus.orchestration.progressive import _launch_failure_checkpoint_fields
from tests.test_omniroute_exit_code_propagation import (
    _DbMixin,
    _make_popen_side_effect,
)

REPO = r"C:\fake\linked-worktree\worker-0"
RUN_ID = "RUN-PROP-TEST"


def _agent_row(agent_id: str) -> dict:
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT worktree, status, result, provider, model FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
    return dict(row)


class _LaunchDiagnosticsBase(_DbMixin):
    def _call(self, **overrides):
        kwargs = dict(
            run_id=RUN_ID,
            repo=REPO,
            task="do work",
            model="cc/claude-sonnet-5-low",
            effort="low",
        )
        kwargs.update(overrides)
        return run_omniroute_worker(**kwargs)


class LaunchFailureCaseA_EnvironmentPreparation(_LaunchDiagnosticsBase):
    """CASE A: codex_subprocess_env() raises; the process must never start."""

    def test_environment_failure_persists_worktree_phase_and_exception(self):
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            side_effect=RuntimeError("synthetic environment failure"),
        ), patch("nexus.dispatchers.omniroute.subprocess.Popen") as popen:
            result = self._call()

        popen.assert_not_called()
        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertEqual(result["reason"], LAUNCH_FAILED_REASON)
        self.assertEqual(result["phase"], "ENVIRONMENT_SETUP")
        self.assertEqual(result["exception_type"], "RuntimeError")
        self.assertEqual(result["error"], "synthetic environment failure")
        self.assertEqual(result["worktree"], REPO)

        row = _agent_row(result["agent_id"])
        self.assertEqual(row["worktree"], REPO)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["provider"], "omniroute")
        self.assertEqual(row["model"], "cc/claude-sonnet-5-low")
        lines = row["result"].splitlines()
        self.assertEqual(lines[0], LAUNCH_FAILED_REASON)
        self.assertIn("phase=ENVIRONMENT_SETUP", lines)
        self.assertIn("exception_type=RuntimeError", lines)
        self.assertIn("message=synthetic environment failure", lines)
        self.assertIn(f"agent_id={result['agent_id']}", lines)
        self.assertIn("execution_path=OMNIROUTE", lines)

    def test_worktree_is_persisted_before_environment_setup(self):
        """The worktree row must already exist when codex_subprocess_env runs."""
        seen_worktree = {}

        def _env_side_effect(**_kwargs):
            # Read the agent row while preparation is in progress.
            with database.get_connection() as conn:
                seen_worktree["value"] = conn.execute(
                    "SELECT worktree FROM agents ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0]
            raise RuntimeError("stop after observing the row")

        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            side_effect=_env_side_effect,
        ), patch("nexus.dispatchers.omniroute.subprocess.Popen"):
            self._call()

        self.assertEqual(seen_worktree["value"], REPO)


class LaunchFailureCaseB_ProcessStart(_LaunchDiagnosticsBase):
    """CASE B: environment succeeds; process creation raises."""

    def test_process_start_failure_records_phase_and_exception(self):
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            return_value={},
        ), patch(
            "nexus.dispatchers.omniroute.subprocess.Popen",
            side_effect=OSError("synthetic process failure"),
        ):
            result = self._call()

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertEqual(result["phase"], "PROCESS_START")
        self.assertEqual(result["exception_type"], "OSError")
        self.assertEqual(result["error"], "synthetic process failure")

        row = _agent_row(result["agent_id"])
        self.assertEqual(row["worktree"], REPO)
        self.assertIn("phase=PROCESS_START", row["result"])
        self.assertIn("exception_type=OSError", row["result"])
        self.assertIn("message=synthetic process failure", row["result"])


class LaunchFailureCaseC_Success(_LaunchDiagnosticsBase):
    """CASE C: a successful Worker gets no launch diagnostic."""

    def test_success_has_no_launch_diagnostic(self):
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            return_value={},
        ), patch(
            "nexus.dispatchers.omniroute.subprocess.Popen",
            side_effect=_make_popen_side_effect(
                returncode=0,
                file_content="Worker completed the task.",
                stdout_lines=["Model : cc/claude-sonnet-5-low\n"],
            ),
        ):
            result = self._call()

        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("reason", result)
        self.assertNotIn("phase", result)
        self.assertNotIn("exception_type", result)
        self.assertEqual(result["worktree"], REPO)

        row = _agent_row(result["agent_id"])
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["worktree"], REPO)
        self.assertFalse(row["result"].startswith(LAUNCH_FAILED_REASON))

    def test_success_adds_no_launch_fields_to_checkpoint(self):
        self.assertEqual(
            _launch_failure_checkpoint_fields(
                {"status": "COMPLETED", "worktree": REPO, "agent_id": "A"}, 1
            ),
            {},
        )


class LaunchFailureCaseC_AdapterMissing(_LaunchDiagnosticsBase):
    """A missing wrapper is a launch failure in the WRAPPER_PREP phase."""

    def test_missing_adapter_is_recorded_in_wrapper_prep(self):
        missing = self._tmp.name + r"\no-such-omniroute-worker.ps1"
        with patch.object(omniroute, "ADAPTER_PATH", type(omniroute.ADAPTER_PATH)(missing)), patch(
            "nexus.dispatchers.omniroute.subprocess.Popen"
        ) as popen:
            result = self._call()

        popen.assert_not_called()
        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertEqual(result["phase"], "WRAPPER_PREP")
        self.assertEqual(result["exception_type"], "FileNotFoundError")
        self.assertEqual(_agent_row(result["agent_id"])["worktree"], REPO)


class LaunchFailureCaseE_UnexpectedException(_LaunchDiagnosticsBase):
    """Unexpected exception types keep their propagation; only persistence is added."""

    def test_unexpected_exception_is_persisted_then_reraised(self):
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            side_effect=ValueError("unexpected defect"),
        ), patch("nexus.dispatchers.omniroute.subprocess.Popen") as popen:
            with self.assertRaises(ValueError):
                self._call()

        popen.assert_not_called()
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT id, worktree, status, result FROM agents "
                "WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                (RUN_ID,),
            ).fetchone()
        self.assertEqual(row["worktree"], REPO)
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("phase=ENVIRONMENT_SETUP", row["result"])
        self.assertIn("exception_type=ValueError", row["result"])


class LaunchFailureCaseD_Safety(_LaunchDiagnosticsBase):
    """CASE D: serialized diagnostics never carry env values, auth or tokens."""

    SECRET_PARTS = ("sk-LEAKME123456", "tok_LEAKME987", "zz-env-LEAKME-555")

    def test_credentials_in_exception_text_are_redacted(self):
        leaky = (
            "could not read config api_key=sk-LEAKME123456 "
            "Authorization: Bearer tok_LEAKME987 "
            "token=zz-env-LEAKME-555"
        )
        with patch.dict(os.environ, {"NEXUS_TEST_LEAK": "zz-env-LEAKME-555"}), patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            side_effect=RuntimeError(leaky),
        ), patch("nexus.dispatchers.omniroute.subprocess.Popen"):
            result = self._call()

        row = _agent_row(result["agent_id"])
        serialized = " ".join([row["result"], str(result)])
        for secret in self.SECRET_PARTS:
            self.assertNotIn(secret, serialized)
        self.assertIn("<redacted>", result["error"])
        self.assertNotIn("PATH=", row["result"])
        self.assertNotIn("CODEX_HOME=", row["result"])

    def test_message_is_single_line_and_bounded(self):
        noisy = "line one\nline two " + ("x" * 2000)
        message = _safe_launch_message(RuntimeError(noisy))
        self.assertNotIn("\n", message)
        self.assertLessEqual(len(message), omniroute._LAUNCH_MESSAGE_LIMIT + 3)
        self.assertTrue(message.endswith("..."))


class ProgressiveCheckpointFieldsTests(unittest.TestCase):
    """The WORKER_ATTEMPT payload carries the safe launch fields for a launch failure."""

    def test_launch_failure_fields_are_emitted(self):
        worker_result = {
            "agent_id": "AGENT-LF0001",
            "status": "LAUNCH_FAILED",
            "reason": LAUNCH_FAILED_REASON,
            "phase": "ENVIRONMENT_SETUP",
            "exception_type": "RuntimeError",
            "error": "synthetic environment failure",
            "worktree": REPO,
        }
        fields = _launch_failure_checkpoint_fields(worker_result, worker_ordinal=1)
        self.assertEqual(
            fields,
            {
                "reason": LAUNCH_FAILED_REASON,
                "phase": "ENVIRONMENT_SETUP",
                "exception_type": "RuntimeError",
                "error": "synthetic environment failure",
                "worker_ordinal": 1,
            },
        )

    def test_other_failures_get_no_launch_fields(self):
        self.assertEqual(
            _launch_failure_checkpoint_fields(
                {"status": "FAILED", "reason": "WORKER_TIMEOUT"}, worker_ordinal=2
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
