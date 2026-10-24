"""
tests/test_phase4_9_sandbox_capability.py

Phase 4.9.1: Codex sandbox capability regression tests.

The installed Codex CLI's `-s/--sandbox` flag only accepts three values:
read-only, workspace-write, danger-full-access. There is no "elevated"
Windows sandbox mode (confirmed via `codex features list`:
elevated_windows_sandbox and experimental_windows_sandbox are both
stage=removed, enabled=false). Nexus policy is:

    normal workers -> sandbox_mode = "workspace-write"
    forbidden      -> sandbox_mode = "danger-full-access"

These tests prove that policy is enforced at both layers that can see a
sandbox mode before a real Codex process is ever spawned:

  1. nexus.dispatchers.omniroute.run_omniroute_worker -- the Python call
     boundary used by every worker/scout dispatch path.
  2. nexus.process_env._validate_omniroute_data -- the OmniRoute Worker
     config validator that gates codex_subprocess_env(profile="omniroute").

No real Codex CLI process is invoked; subprocess/agent-registry calls are
mocked so these are pure containment tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.dispatchers.omniroute import run_omniroute_worker
from nexus.process_env import _validate_omniroute_data


# ---------------------------------------------------------------------------
# Layer 1: run_omniroute_worker sandbox_mode gate
# ---------------------------------------------------------------------------

def test_workspace_write_is_accepted_and_reaches_subprocess_launch():
    """workspace-write is the normal-worker mode and must proceed to launch."""
    with patch(
        "nexus.dispatchers.omniroute.create_agent", return_value="AGENT-WW"
    ), patch(
        "nexus.dispatchers.omniroute.update_agent_status"
    ), patch(
        "nexus.dispatchers.omniroute.update_agent_execution"
    ), patch(
        "nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}
    ), patch(
        "nexus.dispatchers.omniroute.subprocess.Popen"
    ) as mock_popen:
        mock_process = MagicMock()
        mock_process.stdout = iter([])
        mock_process.wait.return_value = 0
        mock_popen.return_value = mock_process

        run_omniroute_worker(
            run_id="RUN-WW",
            repo="C:/fake/repo",
            task="do something",
            model="cc/claude-sonnet-5-low",
            sandbox_mode="workspace-write",
        )

    mock_popen.assert_called_once()
    launched_command = mock_popen.call_args.args[0]
    assert "-Sandbox" in launched_command
    sandbox_index = launched_command.index("-Sandbox")
    assert launched_command[sandbox_index + 1] == "workspace-write"


def test_read_only_is_accepted_and_reaches_subprocess_launch():
    """read-only (scouts/reviewers) remains an accepted mode."""
    with patch(
        "nexus.dispatchers.omniroute.create_agent", return_value="AGENT-RO"
    ), patch(
        "nexus.dispatchers.omniroute.update_agent_status"
    ), patch(
        "nexus.dispatchers.omniroute.update_agent_execution"
    ), patch(
        "nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}
    ), patch(
        "nexus.dispatchers.omniroute.subprocess.Popen"
    ) as mock_popen:
        mock_process = MagicMock()
        mock_process.stdout = iter([])
        mock_process.wait.return_value = 0
        mock_popen.return_value = mock_process

        run_omniroute_worker(
            run_id="RUN-RO",
            repo="C:/fake/repo",
            task="inspect something",
            model="cc/claude-sonnet-5-low",
            sandbox_mode="read-only",
        )

    mock_popen.assert_called_once()


def test_danger_full_access_is_rejected_before_any_agent_or_process_is_created():
    """danger-full-access must fail closed immediately: no agent row, no subprocess."""
    with patch("nexus.dispatchers.omniroute.create_agent") as mock_create_agent, patch(
        "nexus.dispatchers.omniroute.subprocess.Popen"
    ) as mock_popen:
        with pytest.raises(ValueError, match="Unsupported Worker sandbox mode"):
            run_omniroute_worker(
                run_id="RUN-DANGER",
                repo="C:/fake/repo",
                task="do anything",
                model="cc/claude-sonnet-5-low",
                sandbox_mode="danger-full-access",
            )

    mock_create_agent.assert_not_called()
    mock_popen.assert_not_called()


def test_unknown_sandbox_mode_string_is_rejected_before_launch():
    """A typo'd or fabricated sandbox string must also fail closed, not just the
    one known-dangerous literal."""
    with patch("nexus.dispatchers.omniroute.create_agent") as mock_create_agent, patch(
        "nexus.dispatchers.omniroute.subprocess.Popen"
    ) as mock_popen:
        with pytest.raises(ValueError, match="Unsupported Worker sandbox mode"):
            run_omniroute_worker(
                run_id="RUN-TYPO",
                repo="C:/fake/repo",
                task="do anything",
                model="cc/claude-sonnet-5-low",
                sandbox_mode="elevated",
            )

    mock_create_agent.assert_not_called()
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Layer 2: process_env._validate_omniroute_data config gate
# ---------------------------------------------------------------------------

def _base_config(**overrides) -> dict:
    config = {
        "model_provider": "omniroute",
        "model_providers": {
            "omniroute": {"base_url": "https://example.invalid", "env_key": "OMNIROUTE_KEY"}
        },
        "sandbox_mode": "workspace-write",
        "approval_policy": "never",
    }
    config.update(overrides)
    return config


def test_validate_omniroute_data_accepts_workspace_write():
    """workspace-write + approval_policy=never is the only accepted Worker config."""
    _validate_omniroute_data(_base_config())  # must not raise


def test_validate_omniroute_data_rejects_danger_full_access():
    with pytest.raises(RuntimeError, match="workspace-write"):
        _validate_omniroute_data(_base_config(sandbox_mode="danger-full-access"))


def test_validate_omniroute_data_fails_closed_when_sandbox_mode_missing():
    """A config dict with no sandbox_mode key at all must fail closed, not
    silently default to something permissive."""
    config = _base_config()
    del config["sandbox_mode"]
    with pytest.raises(RuntimeError, match="workspace-write"):
        _validate_omniroute_data(config)


def test_validate_omniroute_data_fails_closed_when_approval_policy_missing():
    config = _base_config()
    del config["approval_policy"]
    with pytest.raises(RuntimeError, match="auto-escalate"):
        _validate_omniroute_data(config)


def test_validate_omniroute_data_rejects_read_only_for_worker_profile():
    """The OmniRoute Worker profile is workspace-write only; read-only is a
    dispatcher-level scout/reviewer mode, not a valid Worker config value."""
    with pytest.raises(RuntimeError, match="workspace-write"):
        _validate_omniroute_data(_base_config(sandbox_mode="read-only"))
