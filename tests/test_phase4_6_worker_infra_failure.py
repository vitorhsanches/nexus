"""Phase 4.6: Worker failure propagation + deterministic infrastructure
fail-fast.

Real E2E defect (inventory-lite-e2e-5, PROGRAM-08CF0031): a Worker's Codex
process exited nonzero because of a Windows sandbox/tool-process bug
("Failed to create unified exec process: pin C:\\"). Nexus discarded the
real diagnostic text (only "[ENVIRONMENT_FAILURE] WORKER_EXECUTION_FAILED
no further detail" reached the Program) and spent its full 4-attempt
correction budget re-running an identical Manager+Worker plan against a
failure no repository change could ever fix.

Layer 1: nexus/dispatchers/omniroute.py's Popen-mocked contract (reusing
the exact harness tests/test_omniroute_exit_code_propagation.py already
uses) -- proves the root diagnostic text is now returned, not just
persisted to the Agent registry and discarded from the caller's result.

Layer 2: nexus/failure_classification.py -- proves the new reason still
classifies as ENVIRONMENT_FAILURE (existing taxonomy, per Phase 4.6's own
requirement), still describes with real evidence, and is recognized by the
new narrow fail-fast predicate -- while everything else stays untouched.

Layer 3 (real path): nexus/program/runner.py's _run_stage, driven through
run_go/FakeGo exactly like the rest of the Program test suite -- proves a
deterministic Worker infrastructure failure blocks after exactly one
attempt (Manager invoked once), while TRANSIENT_PROVIDER-classified GoErrors
still get the existing implement-mission retry behavior unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus import failure_classification as fc
from nexus.orchestration.go import GoError
from tests.test_omniroute_exit_code_propagation import _DbMixin, _make_popen_side_effect
from tests.test_program import FakeGo, isolated, program_env  # noqa: F401 -- fixtures reused


REAL_SANDBOX_ERROR_TEXT = "Failed to create unified exec process: pin C:\\"


# ---------------------------------------------------------------------------
# Layer 1: nexus/dispatchers/omniroute.py
# ---------------------------------------------------------------------------


class WorkerProcessFailureDetailTestCase(_DbMixin):
    def _call(self, stdout_lines):
        from nexus.dispatchers.omniroute import run_omniroute_worker

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_make_popen_side_effect(
                    returncode=1, file_content=None, stdout_lines=stdout_lines,
                ),
            ),
        ):
            mock_path.exists.return_value = True
            return run_omniroute_worker(
                run_id="RUN-PROP-TEST", repo=r"C:\fake\repo", task="do work",
                model="cc/claude-sonnet-5-low",
            )

    def test_nonzero_nontransient_exit_is_named_worker_process_failure(self):
        result = self._call([REAL_SANDBOX_ERROR_TEXT + "\n"])
        assert result["status"] == "FAILED"
        assert result["reason"] == fc.WORKER_PROCESS_FAILURE

    def test_root_diagnostic_text_is_returned_not_just_persisted(self):
        """The exact bug: the real error text must reach the CALLER's result
        dict, not only nexus.registry.agents' persisted `result` column."""
        result = self._call([REAL_SANDBOX_ERROR_TEXT + "\n"])
        assert "error" in result
        assert REAL_SANDBOX_ERROR_TEXT in result["error"]

    def test_transient_marker_still_wins_over_the_new_reason(self):
        """Regression: a recognized transient marker must still classify as
        TRANSIENT_PROVIDER_ERROR, never demoted to WORKER_PROCESS_FAILURE."""
        result = self._call(["503 Service Unavailable\n"])
        assert result["reason"] == "TRANSIENT_PROVIDER_ERROR"

    def test_completed_result_never_carries_an_error_field(self):
        from nexus.dispatchers.omniroute import run_omniroute_worker

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_make_popen_side_effect(
                    returncode=0, file_content="done", stdout_lines=["ok\n"],
                ),
            ),
        ):
            mock_path.exists.return_value = True
            result = run_omniroute_worker(
                run_id="RUN-PROP-TEST", repo=r"C:\fake\repo", task="do work",
                model="cc/claude-sonnet-5-low",
            )
        assert result["status"] == "COMPLETED"
        assert "error" not in result

    def test_error_detail_never_contains_env_values(self):
        """codex_subprocess_env() is mocked to {} by _DbMixin and its real
        values are never printed to stdout by anything under test -- the
        returned "error" field is built exclusively from the subprocess's
        own stdout/stderr transcript, so a value that only ever lived in
        the environment can never leak into it."""
        secret_marker = "NEXUS_TEST_SECRET_TOKEN_MUST_NOT_LEAK"
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            return_value={"OMNIROUTE_AUTH_TOKEN": secret_marker},
        ):
            result = self._call([REAL_SANDBOX_ERROR_TEXT + "\n"])
        assert secret_marker not in result.get("error", "")


# ---------------------------------------------------------------------------
# Layer 2: nexus/failure_classification.py
# ---------------------------------------------------------------------------


def _worker_execution_failed_error(reason, error_text=None, run_id="RUN-1"):
    detail = {"worker": {"status": "FAILED", "reason": reason}}
    if error_text is not None:
        detail["worker"]["error"] = error_text
    return GoError("WORKER_EXECUTION_FAILED", "failed", run_id=run_id, detail=detail)


def test_worker_process_failure_classifies_as_environment_failure():
    error = _worker_execution_failed_error(fc.WORKER_PROCESS_FAILURE, REAL_SANDBOX_ERROR_TEXT)
    assert fc.classify_go_error(error) == fc.ENVIRONMENT_FAILURE


def test_description_includes_layer_and_real_detail_not_no_further_detail():
    error = _worker_execution_failed_error(fc.WORKER_PROCESS_FAILURE, REAL_SANDBOX_ERROR_TEXT)
    description = fc.describe_go_error(error)
    assert "no further detail" not in description
    assert "layer=worker_runtime/codex_tool_process" in description
    assert REAL_SANDBOX_ERROR_TEXT in description


@pytest.mark.parametrize(
    "reason,expected",
    [
        (fc.WORKER_PROCESS_FAILURE, True),
        (fc.WORKER_LAUNCH_ENCODING_FAILURE, True),
        ("WORKER_RESULT_MISSING", False),
        ("WORKER_RESULT_EMPTY", False),
        ("TRANSIENT_PROVIDER_ERROR", False),
        ("WORKER_TIMEOUT", False),
        (None, False),
    ],
)
def test_is_deterministic_worker_infrastructure_failure_is_narrow(reason, expected):
    error = _worker_execution_failed_error(reason)
    assert fc.is_deterministic_worker_infrastructure_failure(error) is expected


def test_predicate_false_for_non_worker_codes():
    review_error = GoError(
        "REVIEW_BLOCKED", "failed", run_id="RUN-1",
        detail={"review": {"failure_class": "REQUIREMENT_FAILURE"}},
    )
    assert fc.is_deterministic_worker_infrastructure_failure(review_error) is False


def test_predicate_false_without_detail():
    error = GoError("WORKER_EXECUTION_FAILED", "failed", run_id="RUN-1")
    assert fc.is_deterministic_worker_infrastructure_failure(error) is False


def test_generic_environment_failure_sources_are_not_swept_into_fail_fast():
    """Regression: only the new, narrow worker reason is fail-fast-eligible
    via the new predicate -- other ENVIRONMENT_FAILURE-classified GoErrors
    (e.g. GIT_TOOL_FAILURE) are untouched by this predicate."""
    error = GoError("GIT_TOOL_FAILURE", "failed", run_id="RUN-1")
    assert fc.classify_go_error(error) == fc.ENVIRONMENT_FAILURE
    assert fc.is_deterministic_worker_infrastructure_failure(error) is False
    assert fc.blocks_coding_retry(fc.ENVIRONMENT_FAILURE) is False


# ---------------------------------------------------------------------------
# Layer 3 (real path): Program runner._run_stage
# ---------------------------------------------------------------------------


def test_deterministic_worker_infrastructure_failure_blocks_after_one_attempt(program_env):
    """The exact regression: 4 identical attempts -> now exactly 1."""
    program_env["go"].outcomes = [
        (
            "WORKER_EXECUTION_FAILED",
            {"worker": {"status": "FAILED", "reason": fc.WORKER_PROCESS_FAILURE, "error": REAL_SANDBOX_ERROR_TEXT}},
        )
    ]

    from nexus.program import runner

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Infra", "reqs", program_env["workspace"])

    assert len(program_env["go"].missions) == 1
    blocked = _load(info.value.program_id)
    assert blocked["phases"][0]["attempts"] == 1
    assert blocked["phases"][0]["last_failure_class"] == fc.ENVIRONMENT_FAILURE
    assert REAL_SANDBOX_ERROR_TEXT in blocked["blocked_reason"]
    assert "layer=worker_runtime/codex_tool_process" in blocked["blocked_reason"]


def _load(program_id):
    from nexus.program import state
    return state.load_program(program_id)


def test_manager_is_not_repeatedly_invoked_for_terminal_infra_failure(program_env):
    program_env["go"].outcomes = [
        (
            "WORKER_EXECUTION_FAILED",
            {"worker": {"status": "FAILED", "reason": fc.WORKER_PROCESS_FAILURE, "error": REAL_SANDBOX_ERROR_TEXT}},
        )
    ]
    from nexus.program import runner

    with pytest.raises(runner.ProgramBlockedError):
        runner.start_program("Infra2", "reqs", program_env["workspace"])

    # Exactly one run_go call was made for the whole phase -- no second,
    # third, or fourth "essentially identical" attempt.
    assert len(program_env["go"].missions) == 1


def test_transient_provider_still_retries_same_mission_not_blocked(program_env):
    """Regression: a TRANSIENT_PROVIDER-classified GoError (the existing
    same-tier-retry-then-fallback layer already exhausted itself before
    this GoError ever reached Program) must still use the existing fresh
    implement-mission retry, never the new deterministic-infra fail-fast
    gate -- distinct from WORKER_PROCESS_FAILURE despite both originating
    from WORKER_EXECUTION_FAILED."""
    program_env["go"].outcomes = [
        (
            "WORKER_EXECUTION_FAILED",
            {"worker": {"status": "FAILED", "reason": "TRANSIENT_PROVIDER_ERROR"}},
        )
    ]

    from nexus.program import runner

    program = runner.start_program("Transient", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    assert len(program_env["go"].missions) >= 2  # phase 1 retried once, not fail-fast blocked


def test_route_unavailable_still_fails_fast(program_env, monkeypatch):
    from nexus.orchestration.go import GoError as _GoError
    from nexus.program import runner

    class RouteUnavailableGo(FakeGo):
        def __call__(self, mission, project_query=None, planning_required=False, exclude_models=frozenset()):
            self.missions.append(mission)
            raise _GoError("INITIAL_ROUTING_UNAVAILABLE", "no eligible route", run_id="RUN-X")

    go = RouteUnavailableGo()
    monkeypatch.setattr(runner, "run_go", go)

    with pytest.raises(runner.ProgramBlockedError):
        runner.start_program("RouteGone", "reqs", program_env["workspace"])

    assert len(go.missions) == 1

def test_pre_worker_encoding_failure_blocks_after_one_attempt(program_env):
    detail = "UnicodeEncodeError: 'charmap' codec can't encode character '\\u2192' in position 6"
    program_env["go"].outcomes = [
        ("WORKER_EXECUTION_FAILED", {"worker": {
            "status": "FAILED", "reason": fc.WORKER_LAUNCH_ENCODING_FAILURE,
            "error": detail,
        }}),
    ]
    from nexus.program import runner
    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("PreWorkerEncoding", "reqs", program_env["workspace"])
    blocked = _load(info.value.program_id)
    assert blocked["phases"][0]["attempts"] == 1
    assert len(program_env["go"].missions) == 1
    assert detail in blocked["blocked_reason"]
    assert blocked["owner_pid"] is None


def test_terminal_program_exception_releases_ownership(program_env, monkeypatch):
    from nexus.program import runner, state
    def crash(*args, **kwargs):
        raise RuntimeError("terminal test error")
    monkeypatch.setattr(runner, "_specify", crash)
    with pytest.raises(RuntimeError, match="terminal test error"):
        runner.start_program("TerminalCleanup", "reqs", program_env["workspace"])
    # The program exists even though start_program raised.
    from pathlib import Path
    records = list(Path(state.PROGRAMS_DIR).glob("*.json"))
    assert records
    program = state.load_program(records[0].stem)
    assert program["status"] == "BLOCKED"
    assert program["owner_pid"] is None

def test_worker_banner_encodes_on_legacy_windows_console(monkeypatch):
    import io
    import sys
    from nexus.orchestration import executor
    output = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(executor, "run_omniroute_worker", lambda **kwargs: {"status": "COMPLETED"})
    worker = {
        "execution_path": "OMNIROUTE", "scope": "test", "route_class": "test",
        "provider": "test", "model": "test", "effort": "low",
    }
    result = executor.execute_worker("RUN-1", "repo", "manager", worker)
    assert result["status"] == "COMPLETED"
    output.flush()
    assert b"NEXUS -> WORKER 1" in output.buffer.getvalue()
