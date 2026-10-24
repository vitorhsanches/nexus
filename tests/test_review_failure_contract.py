import unittest
from pathlib import Path
from unittest.mock import patch

from nexus import failure_classification as fc
from nexus.dispatchers.review import review_worker


class _CapturingProcess:
    def __init__(self):
        self.stdout = iter(())

    def wait(self):
        return 0


class _GoError(Exception):
    def __init__(self, code, detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _review_error(failure_class):
    return _GoError(
        "REVIEW_BLOCKED",
        {"review": {"verdict": "BLOCKED", "failure_class": failure_class}},
    )


class ReviewFailureContractTests(unittest.TestCase):
    def _captured_prompt(self):
        commands = []

        def launcher(command):
            commands.append(command)
            return _CapturingProcess()

        with patch("nexus.dispatchers.review._find_codex", return_value=Path("codex.exe")), patch(
            "nexus.dispatchers.review.create_agent", return_value="AGENT-1"
        ), patch("nexus.dispatchers.review.update_agent_status"), patch(
            "nexus.dispatchers.review.update_agent_execution"
        ):
            review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                model="cc/claude-sonnet-5-high",
                effort="high",
                risk_level="medium",
                process_launcher=launcher,
            )
        return commands[0][-1]

    def test_prompt_defines_host_restrictions_as_tool_failure_not_capability(self):
        prompt = self._captured_prompt()
        self.assertIn("TOOL_FAILURE: the host, sandbox, executable", prompt)
        self.assertIn("CAPABILITY_FAILURE: the selected model itself", prompt)
        self.assertIn("never CAPABILITY_FAILURE", prompt)

    def test_case_a_environment_failure_is_not_capability_insufficient(self):
        failure = fc.classify_go_error(_review_error("TOOL_FAILURE"))
        self.assertEqual(failure, fc.ENVIRONMENT_FAILURE)
        self.assertNotEqual(failure, fc.CAPABILITY_INSUFFICIENT)

    def test_case_b_genuine_capability_failure_still_excludes_model(self):
        failure = fc.classify_go_error(_review_error("CAPABILITY_FAILURE"))
        self.assertEqual(failure, fc.CAPABILITY_INSUFFICIENT)

    def test_case_c_requirement_failure_keeps_requirement_semantics(self):
        failure = fc.classify_go_error(_review_error("REQUIREMENT_FAILURE"))
        self.assertEqual(failure, fc.REQUIREMENT_UNSATISFIED)
        self.assertNotEqual(failure, fc.CAPABILITY_INSUFFICIENT)


if __name__ == "__main__":
    unittest.main()
