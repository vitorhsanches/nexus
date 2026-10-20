import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import nexus.registry.database as database
from nexus.registry.agents import create_agent


class _FakeRoutingService:
    def __init__(self, decision):
        self._decision = decision

    def select_route_for_capability(self, capability, risk_level=None):
        return self._decision


class DispatcherLaunchFailureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        database.initialize_database()

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO projects (id, name, path, aliases_json, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("norte", "Norte", r"C:\fake\interface-life", "[]", 1),
            )
            connection.execute(
                """
                INSERT INTO runs (id, project_id, input, intent, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("RUN-TEST001", "norte", "task", "GO", "RUNNING", "now"),
            )

        self.worker_agent_id = create_agent(
            run_id="RUN-TEST001",
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="COMPLETED",
        )

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        self._tmp_dir.cleanup()

    def test_manager_missing_codex_returns_launch_failed(self):
        from nexus.dispatchers.manager import run_manager

        with patch(
            "nexus.dispatchers.manager._find_codex",
            side_effect=FileNotFoundError("Codex executable not found"),
        ):
            result = run_manager(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertIn("Codex executable not found", result["error"])

    def test_manager_routes_through_omniroute_profile_not_codex_oauth(self):
        """NEXUS_UNIFIED_OMNIROUTE_EXECUTION: the Manager must no longer
        depend on the user's interactive Codex ChatGPT OAuth session --
        it launches its subprocess through codex_subprocess_env's isolated
        "omniroute" profile, exactly like Worker and Reviewer already do,
        never the "default" profile (which mirrors ~/.codex and has no
        [model_providers.omniroute] table at all)."""
        from nexus.dispatchers.manager import run_manager

        fake_process = MagicMock()
        fake_process.stdout = io.StringIO(
            "NEXUS_PLAN_BEGIN\n"
            '{"complexity":"LOW","risk":"LOW","parallelism":1,'
            '"summary":"s","intent":"ANALYSIS","workers":[],'
            '"risk_consistency_notice":null}\n'
            "NEXUS_PLAN_END\n"
        )
        fake_process.wait.return_value = 0

        captured_profiles = []

        def fake_codex_subprocess_env(profile="default"):
            captured_profiles.append(profile)
            return {}

        with patch(
            "nexus.dispatchers.manager._find_codex", return_value=Path("/fake/codex"),
        ), patch(
            "nexus.dispatchers.manager.subprocess.Popen", return_value=fake_process,
        ), patch(
            "nexus.dispatchers.manager.codex_subprocess_env",
            side_effect=fake_codex_subprocess_env,
        ):
            result = run_manager(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(captured_profiles, ["omniroute"])

    def test_manager_agent_provider_is_omniroute_not_codex(self):
        """The persisted Manager agent's `provider` must be "omniroute" (the
        model gateway), never "codex" -- Codex CLI is the execution engine,
        not the upstream model provider (Section 10 of the OmniRoute
        unification work: do not misrepresent the engine as the provider)."""
        from nexus.dispatchers.manager import run_manager
        from nexus.registry.agents import get_agent

        fake_process = MagicMock()
        fake_process.stdout = io.StringIO(
            "NEXUS_PLAN_BEGIN\n"
            '{"complexity":"LOW","risk":"LOW","parallelism":1,'
            '"summary":"s","intent":"ANALYSIS","workers":[],'
            '"risk_consistency_notice":null}\n'
            "NEXUS_PLAN_END\n"
        )
        fake_process.wait.return_value = 0

        with patch(
            "nexus.dispatchers.manager._find_codex", return_value=Path("/fake/codex"),
        ), patch(
            "nexus.dispatchers.manager.subprocess.Popen", return_value=fake_process,
        ), patch(
            "nexus.dispatchers.manager.codex_subprocess_env", return_value={},
        ):
            result = run_manager(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
            )

        agent = get_agent(result["manager_id"])
        self.assertEqual(agent["provider"], "omniroute")

    def test_manager_nonzero_exit_reports_bounded_redacted_diagnostic(self):
        """RUN-4EFBBB22 regression: a Manager subprocess that runs and
        exits non-zero (e.g. a Codex auth/token failure) must surface a
        bounded, credential-redacted root cause in result["error"] --
        not just an exit code -- even though the real diagnostic is deep
        inside a transcript that is mostly echoed prompt boilerplate."""
        from nexus.dispatchers.manager import run_manager

        transcript = (
            "...entire echoed prompt here, hundreds of lines...\n"
            "2026-10-12T23:31:42Z ERROR codex_login::auth::manager: "
            "Failed to refresh token: Your access token could not be "
            "refreshed because your refresh token was already used. "
            "Please log out and sign in again.\n"
            "2026-10-12T23:32:08Z ERROR codex_login::auth::manager: "
            "Failed to refresh token: Your access token could not be "
            "refreshed because your refresh token was already used. "
            "Please log out and sign in again.\n"
            "ERROR: workspace routing discovery unauthorized (401)\n"
        )
        fake_process = MagicMock()
        fake_process.stdout = io.StringIO(transcript)
        fake_process.wait.return_value = 1

        with patch(
            "nexus.dispatchers.manager._find_codex", return_value=Path("/fake/codex"),
        ), patch(
            "nexus.dispatchers.manager.subprocess.Popen", return_value=fake_process,
        ):
            result = run_manager(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 1)
        self.assertIsNotNone(result["error"])
        self.assertIn("refresh", result["error"].lower())
        self.assertNotEqual(result["error"], "exit_code=1")

    def test_manager_watchdog_timeout_still_reports_manager_timeout(self):
        """The watchdog-fired path must keep reporting the distinct
        "MANAGER_TIMEOUT" sentinel, not the new transcript-derived
        diagnostic (there is no useful transcript to extract from -- the
        process was killed for running too long, not for erroring)."""
        from nexus.dispatchers.manager import run_manager
        import nexus.dispatchers.manager as manager_module

        fake_process = MagicMock()
        fake_process.stdout = io.StringIO("still planning...\n")
        fake_process.wait.return_value = 0

        class _FiredWatchdog:
            def __init__(self, process, timeout_seconds):
                self.fired = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch(
            "nexus.dispatchers.manager._find_codex", return_value=Path("/fake/codex"),
        ), patch(
            "nexus.dispatchers.manager.subprocess.Popen", return_value=fake_process,
        ), patch.object(
            manager_module, "ProcessWatchdog", _FiredWatchdog,
        ):
            result = run_manager(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "MANAGER_TIMEOUT")

    def test_review_missing_codex_returns_launch_failed(self):
        from nexus.dispatchers.review import review_worker
        from nexus.routing.service import RoutingDecision

        fake_routing_service = _FakeRoutingService(
            RoutingDecision(
                model="cc/claude-sonnet-5-low",
                provider="claude",
                effort="low",
                execution_path="OMNIROUTE",
                reason="test double",
                degraded=False,
            )
        )

        with patch(
            "nexus.dispatchers.review._find_codex",
            side_effect=FileNotFoundError("Codex executable not found"),
        ):
            result = review_worker(
                run_id="RUN-TEST001",
                worker_id=self.worker_agent_id,
                worktree=r"C:\fake\worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_routing_service,
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertIn("Codex executable not found", result["error"])

    def test_review_uses_unelevated_windows_sandbox(self):
        """Windows error 1223 regression: the installed Codex CLI's
        "elevated" Windows sandbox mode is a removed feature (confirmed
        via `codex features list`: elevated_windows_sandbox is
        stage=removed, enabled=false), under which every sandboxed shell
        tool call -- git status, git diff, even echo -- fails outright
        ("Failed to create unified exec process"), so the Reviewer could
        never actually inspect anything and always fell back to
        TOOL_FAILURE. The command Reviewer launches must say
        "unelevated" -- the same setting already proven correct for the
        Worker's sandbox (nexus.process_env._safe_omniroute_config) --
        and must never again say "elevated"."""
        from nexus.dispatchers.review import review_worker
        from nexus.routing.service import RoutingDecision

        fake_routing_service = _FakeRoutingService(
            RoutingDecision(
                model="cc/claude-sonnet-5-low",
                provider="claude",
                effort="low",
                execution_path="OMNIROUTE",
                reason="test double",
                degraded=False,
            )
        )

        import io as _io
        fake_output = (
            'NEXUS_REVIEW_BEGIN\n'
            '{"verdict":"PASS","failure_class":null,"summary":"ok","evidence":["e"]}\n'
            'NEXUS_REVIEW_END\n'
        )
        fake_proc = MagicMock()
        fake_proc.stdout = _io.StringIO(fake_output)
        fake_proc.wait.return_value = 0

        captured_commands = []

        def fake_launcher(cmd):
            captured_commands.append(cmd)
            return fake_proc

        with patch(
            "nexus.dispatchers.review._find_codex", return_value=Path("/fake/codex"),
        ):
            review_worker(
                run_id="RUN-TEST001",
                worker_id=self.worker_agent_id,
                worktree=r"C:\fake\worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_routing_service,
                process_launcher=fake_launcher,
            )

        self.assertEqual(len(captured_commands), 1)
        command = captured_commands[0]
        self.assertIn('windows.sandbox="unelevated"', command)
        self.assertNotIn('windows.sandbox="elevated"', command)

    def test_omniroute_missing_adapter_returns_launch_failed(self):
        from nexus.dispatchers.omniroute import run_omniroute_worker

        with patch(
            "nexus.dispatchers.omniroute.ADAPTER_PATH",
        ) as mock_path:
            mock_path.exists.return_value = False

            result = run_omniroute_worker(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
                model="oc/big-pickle",
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertIn("adapter not found", result["error"])


if __name__ == "__main__":
    unittest.main()
