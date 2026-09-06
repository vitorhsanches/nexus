"""Regression tests proving the Manager only approves execution paths that
the runtime executor actually implements.

These tests guard against the Manager and the executor maintaining two
independent execution-path policy lists that can drift out of sync.
"""

import json
import unittest

from nexus.dispatchers.manager import _validate_plan
from nexus.orchestration.executor import (
    KNOWN_UNIMPLEMENTED_EXECUTION_PATHS,
    SUPPORTED_EXECUTION_PATHS,
    PlanExecutionError,
    execute_worker,
)


def _plan_with_execution_path(execution_path):
    return {
        "complexity": "LOW",
        "risk": "LOW",
        "intent": "EXECUTION",
        "parallelism": 1,
        "summary": "Test plan.",
        "workers": [
            {
                "route_class": "mechanical",
                "execution_path": execution_path,
                "provider": "omniroute",
                "model": "oc/big-pickle",
                "effort": "low",
                "scope": "Fix the thing.",
                "reason": "Test.",
            }
        ],
    }


class ExecutionPathParityTestCase(unittest.TestCase):
    """Verifies the Manager's approved paths mirror what the executor
    supports, with no independently drifting allow-list."""

    def test_omniroute_remains_accepted(self):
        plan = _plan_with_execution_path("OMNIROUTE")
        validated = _validate_plan(json.loads(json.dumps(plan)))
        self.assertEqual(
            validated["workers"][0]["execution_path"], "OMNIROUTE"
        )

    def test_native_codex_rejected_during_plan_validation(self):
        plan = _plan_with_execution_path("NATIVE_CODEX")

        with self.assertRaises(ValueError):
            _validate_plan(json.loads(json.dumps(plan)))

    def test_unknown_execution_path_rejected(self):
        plan = _plan_with_execution_path("SOME_MADE_UP_PATH")

        with self.assertRaises(ValueError):
            _validate_plan(json.loads(json.dumps(plan)))

    def test_declared_supported_paths_reflect_executor_reality(self):
        """The Manager's allow-list must be exactly the executor's
        SUPPORTED_EXECUTION_PATHS -- not a second, independently
        maintained list."""
        import nexus.dispatchers.manager as manager

        source = manager.__dict__

        self.assertIn("SUPPORTED_EXECUTION_PATHS", source)
        self.assertEqual(
            source["SUPPORTED_EXECUTION_PATHS"], SUPPORTED_EXECUTION_PATHS
        )
        self.assertIn("OMNIROUTE", SUPPORTED_EXECUTION_PATHS)
        self.assertNotIn("NATIVE_CODEX", SUPPORTED_EXECUTION_PATHS)
        self.assertIn("NATIVE_CODEX", KNOWN_UNIMPLEMENTED_EXECUTION_PATHS)

    def test_manager_prompt_only_advertises_supported_paths(self):
        import inspect

        from nexus.dispatchers import manager

        source = inspect.getsource(manager.run_manager)

        self.assertNotIn("NATIVE_CODEX", source)
        self.assertIn("execution_paths", source)

    def test_executor_rejects_native_codex_before_worker_launch(self):
        worker = _plan_with_execution_path("NATIVE_CODEX")["workers"][0]

        with self.assertRaises(PlanExecutionError):
            execute_worker(
                run_id="RUN-1",
                repo="C:/fake/repo",
                manager_id="AGT-0001",
                worker=worker,
            )


if __name__ == "__main__":
    unittest.main()
