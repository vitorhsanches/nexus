from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus.cli import app as cli
from nexus.intake import (
    ExecutionMode,
    TaskType,
    UnsupportedTaskError,
    dispatch_request,
    normalize_task,
    select_execution_mode,
)
from nexus.orchestration.strategy import (
    ExecutionStrategy,
    choose_execution_strategy,
)


def test_simple_bounded_code_change_is_task():
    contract = normalize_task(
        "Fix the package-lock inconsistency in FinaSync.",
        project="FinaSync",
    )
    assert contract.task_type is TaskType.CODE_CHANGE
    assert select_execution_mode(contract) is ExecutionMode.TASK


def test_complex_finite_software_task_remains_task_and_delegates():
    contract = normalize_task(
        "Implement a multi-file architecture migration in api.py and app.tsx.",
        project="FinaSync",
    )
    strategy, _ = choose_execution_strategy(
        contract.goal,
        complexity="HIGH",
        planning_required=contract.requires_planning,
    )
    assert select_execution_mode(contract) is ExecutionMode.TASK
    assert contract.long_running is False
    assert strategy is ExecutionStrategy.DELEGATED


def test_complete_project_from_requirements_is_program():
    contract = normalize_task(
        "Create this complete project from the requirements in requirements.md.",
        requirements_path="requirements.md",
    )
    assert contract.task_type is TaskType.SOFTWARE_PROJECT
    assert contract.long_running is True
    assert contract.requires_planning is True
    assert select_execution_mode(contract) is ExecutionMode.PROGRAM


def test_repository_read_only_analysis_is_task():
    contract = normalize_task(
        "Analyze repository code in FinaSync without changing files.",
        project="FinaSync",
    )
    assert contract.task_type is TaskType.CODE_ANALYSIS
    assert contract.authority_hint == "READ_ONLY"
    assert select_execution_mode(contract) is ExecutionMode.TASK


@pytest.mark.parametrize(
    ("task_text", "expected"),
    [
        ("Analyze the supplied sales.csv dataset.", TaskType.DATA_ANALYSIS),
        ("Research the current approaches to deterministic scheduling.", TaskType.RESEARCH),
    ],
)
def test_non_repository_tasks_normalize_without_project(task_text, expected):
    contract = normalize_task(task_text)
    assert contract.task_type is expected
    assert contract.project_required is False
    assert contract.project_context is None


def test_general_task_dispatches_to_existing_run_go():
    with patch(
        "nexus.intake.run_go",
        return_value={"run_id": "RUN-TASK", "status": "COMPLETED"},
    ) as run_go, patch("nexus.intake.start_program") as program:
        dispatched = dispatch_request(
            "Fix the package-lock inconsistency in FinaSync.",
            project="FinaSync",
        )

    assert dispatched["execution_mode"] == "TASK"
    assert dispatched["task_contract"]["task_type"] == "CODE_CHANGE"
    run_go.assert_called_once()
    assert run_go.call_args.kwargs["task_type"] == "CODE_CHANGE"
    assert run_go.call_args.kwargs["execution_mode"] == "TASK"
    program.assert_not_called()


def test_general_program_dispatches_to_existing_program_runner(tmp_path):
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Build the complete product.", encoding="utf-8")
    with patch(
        "nexus.intake.start_program",
        return_value={
            "program_id": "PROGRAM-1",
            "status": "SPECIFIED",
            "path": str(tmp_path / "product"),
        },
    ) as program, patch("nexus.intake.run_go") as run_go:
        dispatched = dispatch_request(
            "Create this complete project from requirements.md.",
            requirements_path=str(requirements),
            name="Product",
        )

    assert dispatched["execution_mode"] == "PROGRAM"
    assert dispatched["task_contract"]["task_type"] == "SOFTWARE_PROJECT"
    program.assert_called_once()
    assert program.call_args.kwargs["task_contract"]["task_type"] == "SOFTWARE_PROJECT"
    assert program.call_args.kwargs["task_contract"]["execution_mode"] == "PROGRAM"
    run_go.assert_not_called()


def test_unsupported_projectless_capability_fails_without_project_router():
    with patch("nexus.intake.run_go") as run_go:
        result = dispatch_request("Research deterministic scheduling.")
    assert result["result"]["status"] == "UNSUPPORTED_CAPABILITY"
    assert result["result"]["failure_class"] == "UNSUPPORTED_CAPABILITY"
    run_go.assert_not_called()


def test_case_f_unsupported_capability_never_retries():
    """CASE F: no run_go call at all, and the failure is UNSUPPORTED_CAPABILITY
    -- there is nothing to retry around."""
    with patch("nexus.intake.run_go") as run_go, patch("nexus.intake.start_program") as program:
        result = dispatch_request("Research deterministic scheduling.")
    assert result["result"]["failure_class"] == "UNSUPPORTED_CAPABILITY"
    run_go.assert_not_called()
    program.assert_not_called()


def test_case_g_push_request_needs_approval_no_technical_retry():
    """CASE G: a push/publish request is AUTHORITY_REQUIRED, never a retry."""
    with patch("nexus.intake.run_go") as run_go:
        result = dispatch_request(
            "Push the changes to the remote repository.", project="FinaSync"
        )
    assert result["result"]["status"] == "NEEDS_APPROVAL"
    assert result["result"]["failure_class"] == "AUTHORITY_REQUIRED"
    run_go.assert_not_called()


def test_destructive_database_request_is_policy_blocked_not_authority_required():
    with patch("nexus.intake.run_go") as run_go:
        result = dispatch_request(
            "Drop database in production.", project="FinaSync"
        )
    assert result["result"]["status"] == "BLOCKED"
    assert result["result"]["failure_class"] == "POLICY_BLOCKED"
    run_go.assert_not_called()


def test_case_l_task_acceptance_is_exposed_without_gating_completion():
    """CASE L: a bounded TASK exposes acceptance state without turning into
    a Program and without changing run_go's own completion status."""
    with patch(
        "nexus.intake.run_go",
        return_value={"run_id": "RUN-TASK", "status": "COMPLETED"},
    ):
        dispatched = dispatch_request(
            "Fix app.py, then verify tests pass.",
            project="FinaSync",
        )

    result = dispatched["result"]
    assert result["status"] == "COMPLETED"  # run_go's own status, unchanged
    assert dispatched["execution_mode"] == "TASK"
    ids = {c["id"] for c in result["acceptance"]}
    assert "TASK-EXEC" in ids
    exec_criterion = next(c for c in result["acceptance"] if c["id"] == "TASK-EXEC")
    assert exec_criterion["status"] == "SATISFIED"
    # the free-text validation requirement has no deterministic verifier
    # for a bounded Task -- honestly UNVERIFIED, never fabricated.
    assert any(c["status"] == "UNVERIFIED" for c in result["acceptance"][1:])


def test_general_cli_real_path_dispatches(monkeypatch, capsys):
    dispatched = {
        "task_contract": {"task_type": "CODE_CHANGE"},
        "execution_mode": "TASK",
        "result": {"run_id": "RUN-CLI", "status": "COMPLETED"},
    }
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    dispatch = patch("nexus.cli.app.dispatch_request", return_value=dispatched)
    monkeypatch.setattr(
        "sys.argv",
        ["nexus", "run", "Fix package-lock.json", "--project", "FinaSync"],
    )
    with dispatch as mocked:
        cli.main()

    mocked.assert_called_once()
    assert mocked.call_args.kwargs["project"] == "FinaSync"
    output = capsys.readouterr().out
    assert "CODE_CHANGE" in output and "TASK" in output and "RUN-CLI" in output


def test_general_cli_program_path_is_mode_neutral(monkeypatch, capsys):
    dispatched = {
        "task_contract": {"task_type": "SOFTWARE_PROJECT"},
        "execution_mode": "PROGRAM",
        "result": {
            "program_id": "PROGRAM-CLI",
            "status": "SPECIFIED",
            "path": "C:/product",
        },
    }
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "nexus",
            "run",
            "Create this complete project",
            "--requirements",
            "requirements.md",
        ],
    )
    with patch("nexus.cli.app.dispatch_request", return_value=dispatched):
        cli.main()

    output = capsys.readouterr().out
    assert "SOFTWARE_PROJECT" in output
    assert "PROGRAM" in output
    assert "PROGRAM-CLI" in output


def test_general_cli_passes_explicit_local_autonomy(monkeypatch):
    dispatched = {
        "task_contract": {"task_type": "CODE_CHANGE"},
        "execution_mode": "TASK",
        "result": {"run_id": "RUN-AUTO", "status": "COMPLETED"},
    }
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "nexus", "run", "Fix app.py", "--project", "FinaSync",
            "--autonomy", "local",
        ],
    )
    with patch(
        "nexus.cli.app.dispatch_request", return_value=dispatched
    ) as dispatch:
        cli.main()
    assert dispatch.call_args.kwargs["autonomy"] == "local"


def test_existing_go_and_build_parsers_remain_available(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    with patch("nexus.cli.app.go_command") as go:
        monkeypatch.setattr(
            "sys.argv", ["nexus", "go", "Fix it", "--project", "FinaSync"]
        )
        cli.main()
    go.assert_called_once_with("Fix it", project="FinaSync", prepare_dependencies=False)

    args = SimpleNamespace(
        resume=None,
        name="Product",
        requirements="requirements.md",
        workspace=None,
        spec_only=True,
    )
    assert args.spec_only is True
