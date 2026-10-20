import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nexus.dispatchers.triage import MissionScope, classify_mission_scope
from nexus.orchestration.go import run_go
from nexus.orchestration.strategy import (
    ExecutionStrategy,
    choose_execution_strategy,
    infer_pre_execution_risk,
)
from nexus.telemetry.runs import get_run_summary


def test_bounded_low_risk_metadata_task_is_direct():
    strategy, reason = choose_execution_strategy(
        "Update metadata in nexus/models.py",
        complexity="LOW", risk="LOW",
    )
    assert strategy is ExecutionStrategy.DIRECT
    assert reason == "BOUNDED_LOW_COMPLEXITY"


def test_complex_task_is_delegated():
    strategy, reason = choose_execution_strategy(
        "Implement a multi-step architecture change across the repository",
        complexity="HIGH", risk="LOW",
    )
    assert strategy is ExecutionStrategy.DELEGATED
    assert reason == "REQUIRES_DECOMPOSITION"


def test_high_risk_task_never_uses_direct_shortcut():
    strategy, _ = choose_execution_strategy(
        "Update metadata in nexus/models.py",
        complexity="LOW", risk="HIGH",
    )
    assert strategy is ExecutionStrategy.DELEGATED


def test_planning_requirement_forces_delegated():
    strategy, _ = choose_execution_strategy(
        "Update metadata in nexus/models.py",
        complexity="LOW", risk="LOW", planning_required=True,
    )
    assert strategy is ExecutionStrategy.DELEGATED


def test_coupled_bounded_workstreams_are_delegated():
    strategy, _ = choose_execution_strategy(
        "Apply coupled implementation changes to config in api.py and app.tsx.",
        complexity="LOW",
    )
    assert strategy is ExecutionStrategy.DELEGATED


def test_negated_commit_push_does_not_raise_pre_execution_risk():
    assert infer_pre_execution_risk("Update package-lock.json; do not commit or push.") == "LOW"
    assert infer_pre_execution_risk("Update package-lock.json; no commit/push.") == "LOW"
    assert infer_pre_execution_risk("Não faça commit ou push.") == "LOW"


def test_positive_commit_push_raises_pre_execution_risk():
    assert infer_pre_execution_risk("Update package-lock.json; commit and push the changes.") == "HIGH"
    assert infer_pre_execution_risk("Faça commit e push das alterações.") == "HIGH"
    strategy, _ = choose_execution_strategy(
        "Update package-lock.json; commit and push the changes.",
        complexity="LOW",
        risk="LOW",
    )
    assert strategy is ExecutionStrategy.DELEGATED


class TestRealExecutionEntryPath:
    def setup_method(self):
        import nexus.registry.database as database
        import nexus.registry.projects as projects

        self._tmp_dir = TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)
        self._database = database
        self._projects = projects
        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE
        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"
        projects.PROJECTS_FILE = tmp_path / "projects.json"
        projects.PROJECTS_FILE.write_text(
            json.dumps({
                "projects": [{
                    "id": "finasync",
                    "name": "FinaSync",
                    "path": r"C:\fake\finasync",
                    "aliases": ["finasync"],
                    "enabled": True,
                }]
            }),
            encoding="utf-8",
        )
        database.initialize_database()
        projects.sync_projects()

    def teardown_method(self):
        self._database.DATABASE_PATH = self._original_db_path
        self._database.STORAGE_DIR = self._original_storage_dir
        self._projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    @staticmethod
    def _worker_result():
        return [{
            "status": "COMPLETED",
            "worker": {"status": "COMPLETED", "output": "done"},
            "review": {"verdict": "PASS"},
            "_planned_scope": "requested work",
        }]

    def test_bounded_real_path_is_direct_and_does_not_call_manager(self):
        request = (
            "Synchronize package-lock.json with the existing package.json.\n"
            "- Do not touch application logic, React, Supabase, or persistence.\n"
            "- Run npm ci, npm test, npm run build, and git diff --check.\n"
            "- Return execution telemetry.\n"
            "- No commit/push."
        )
        with patch("nexus.orchestration.go.run_manager") as manager, patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=self._worker_result(),
        ):
            result = run_go(request, project_query="FinaSync")

        summary = get_run_summary(result["run_id"])
        assert classify_mission_scope(request).scope is MissionScope.BOUNDED
        assert result["execution_strategy"] == "DIRECT"
        manager.assert_not_called()
        assert summary["execution_strategy"] == "DIRECT"
        assert not [call for call in summary["calls"] if call["role"] == "MANAGER"]

    def test_bounded_coupled_workstreams_remain_delegated(self):
        plan = {
            "complexity": "HIGH",
            "risk": "LOW",
            "parallelism": 1,
            "intent": "EXECUTION",
            "summary": "Coupled implementation.",
            "workers": [],
        }
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-TEST",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": plan,
            },
        ) as manager, patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=[],
        ):
            result = run_go(
                "Apply coupled implementation changes to config in api.py and app.tsx.",
                project_query="FinaSync",
            )

        assert result["execution_strategy"] == "DELEGATED"
        manager.assert_called_once()

    def test_high_risk_bounded_real_path_cannot_assume_low(self):
        plan = {
            "complexity": "LOW",
            "risk": "HIGH",
            "parallelism": 1,
            "intent": "EXECUTION",
            "summary": "Approved push operation.",
            "workers": [],
        }
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-RISK",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": plan,
            },
        ) as manager, patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=[],
        ):
            result = run_go(
                "Update package-lock.json, then commit and push the changes.",
                project_query="FinaSync",
            )

        assert result["execution_strategy"] == "DELEGATED"
        assert result["plan"]["risk"] == "HIGH"
        manager.assert_called_once()

    def test_broad_real_path_remains_delegated_and_calls_manager(self):
        plan = {
            "complexity": "HIGH",
            "risk": "LOW",
            "parallelism": 1,
            "intent": "EXECUTION",
            "summary": "Delegated architecture work.",
            "workers": [{
                "scope": "Implement the coordinated architecture migration.",
                "reason": "The broad task requires decomposition.",
                "route_class": "advanced-coding",
                "execution_path": "OMNIROUTE",
                "effort": "high",
                "authority": "WORKSPACE_WRITE",
                "deliverable": "Implemented migration.",
                "acceptance_criteria": "Validation passes.",
                "depends_on": [],
            }],
        }
        with patch(
            "nexus.orchestration.go.run_scout_stage",
            return_value="repository evidence",
        ), patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-TEST",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": plan,
            },
        ) as manager, patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=self._worker_result(),
        ):
            result = run_go(
                "Survey this repository, identify the main architecture problems, "
                "and implement the coordinated migration.",
                project_query="FinaSync",
            )

        summary = get_run_summary(result["run_id"])
        assert result["execution_strategy"] == "DELEGATED"
        manager.assert_called_once()
        assert summary["execution_strategy"] == "DELEGATED"
        assert len([call for call in summary["calls"] if call["role"] == "MANAGER"]) == 1
