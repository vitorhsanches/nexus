import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.dispatchers.manager import _validate_plan
from nexus.orchestration.go import run_go
from nexus.registry.runs import get_run
from nexus.router.intent import (
    ANALYSIS,
    EXECUTION,
    PLANNING,
    QUESTION,
    classify_intent,
)


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        },
    ]
}


EXECUTION_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "intent": "EXECUTION",
    "parallelism": 1,
    "summary": "Fix the reported bug.",
    "workers": [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the thing.",
            "reason": "Simple mechanical change.",
        }
    ],
}


ANALYSIS_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "intent": "ANALYSIS",
    "parallelism": 1,
    "summary": "The architecture looks sound; two risks noted.",
    "workers": [],
}


class IntentClassifierTestCase(unittest.TestCase):
    def test_execution_keywords_classify_as_execution(self):
        self.assertEqual(
            classify_intent("Fix the login bug in the auth module"),
            EXECUTION,
        )

    def test_analysis_keywords_classify_as_analysis(self):
        self.assertEqual(
            classify_intent("Analyze the interface-life project"),
            ANALYSIS,
        )

    def test_question_classify_as_question(self):
        self.assertEqual(
            classify_intent("What is the difference between X and Y?"),
            QUESTION,
        )

    def test_planning_keywords_classify_as_planning(self):
        self.assertEqual(
            classify_intent("Create a roadmap for the next quarter"),
            PLANNING,
        )

    def test_empty_text_defaults_to_analysis(self):
        self.assertEqual(classify_intent(""), ANALYSIS)

    def test_execution_overrides_question_wording(self):
        cases = [
            "How do I build this?",
            "Explain how to fix the dashboard bug",
            "What should I change to update this?",
            "Why does this fail and fix it",
            "Can you automate this?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_intent(text), EXECUTION)

    def test_question_without_execution_signal_remains_question(self):
        cases = [
            "What is this?",
            "Why does this happen?",
            "How does this work?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_intent(text), QUESTION)

    def test_every_execution_signal_classifies_as_execution(self):
        signals = [
            "fix",
            "implement",
            "add",
            "create",
            "modify",
            "change",
            "refactor",
            "update",
            "build",
            "write",
            "remove",
            "delete",
            "resolve",
            "automate",
        ]
        for signal in signals:
            with self.subTest(signal=signal):
                self.assertEqual(
                    classify_intent(f"Can you {signal} this for me?"),
                    EXECUTION,
                )

    def test_question_mark_with_execution_request_is_execution(self):
        self.assertEqual(
            classify_intent("Could you please fix this issue?"),
            EXECUTION,
        )


class PlanValidationRulesTestCase(unittest.TestCase):
    def test_execution_plan_requires_workers(self):
        plan = json.loads(json.dumps(EXECUTION_PLAN))
        plan["workers"] = []

        with self.assertRaises(ValueError):
            _validate_plan(plan)

    def test_execution_plan_with_workers_is_valid(self):
        plan = _validate_plan(json.loads(json.dumps(EXECUTION_PLAN)))
        self.assertEqual(plan["intent"], "EXECUTION")

    def test_analysis_plan_without_workers_is_valid(self):
        plan = _validate_plan(json.loads(json.dumps(ANALYSIS_PLAN)))
        self.assertEqual(plan["workers"], [])

    def test_question_plan_forbids_workers(self):
        plan = json.loads(json.dumps(ANALYSIS_PLAN))
        plan["intent"] = "QUESTION"
        plan["workers"] = [EXECUTION_PLAN["workers"][0]]

        with self.assertRaises(ValueError):
            _validate_plan(plan)

    def test_planning_plan_allows_optional_workers(self):
        plan = json.loads(json.dumps(ANALYSIS_PLAN))
        plan["intent"] = "PLANNING"

        validated = _validate_plan(plan)
        self.assertEqual(validated["intent"], "PLANNING")

    def test_invalid_intent_is_rejected(self):
        plan = json.loads(json.dumps(EXECUTION_PLAN))
        plan["intent"] = "NOT_A_REAL_INTENT"

        with self.assertRaises(ValueError):
            _validate_plan(plan)


class GoIntentRoutingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(
            json.dumps(FIXTURE_PROJECTS), encoding="utf-8"
        )
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def test_execution_request_creates_worker_flow(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": EXECUTION_PLAN,
            },
        ) as mock_manager, patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {},
            },
        ) as mock_execute:
            result = run_go("Norte: fix a bug", project_query="norte")

        mock_manager.assert_called_once()
        mock_execute.assert_called_once()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(result["workers"]), 1)

        run = get_run(result["run_id"])
        self.assertEqual(run["status"], "COMPLETED")

    def test_analysis_request_does_not_require_worker(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": ANALYSIS_PLAN,
            },
        ) as mock_manager, patch(
            "nexus.orchestration.go.execute_progressively"
        ) as mock_execute:
            result = run_go("Norte: analyze the architecture", project_query="norte")

        mock_manager.assert_called_once()
        mock_execute.assert_not_called()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["workers"], [])
        self.assertIsNone(result["final_worker"])

        run = get_run(result["run_id"])
        self.assertEqual(run["status"], "COMPLETED")

    def test_invalid_worker_configuration_is_rejected(self):
        invalid_plan = json.loads(json.dumps(EXECUTION_PLAN))
        invalid_plan["workers"][0]["execution_path"] = "NOT_A_REAL_PATH"

        with self.assertRaises(ValueError):
            _validate_plan(invalid_plan)


if __name__ == "__main__":
    unittest.main()
