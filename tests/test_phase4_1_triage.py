"""Phase 4.1 tests -- deterministic mission-scope triage.

Every test that touches the database uses a temporary, disposable copy
(the same nexus.registry.database monkeypatch pattern used by
tests/test_checkpoint_registry.py and tests/test_go_concurrent_phase2.py).
None of them reads or writes nexus/storage/nexus.db.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.dispatchers.triage import MissionScope, classify_mission_scope
from nexus.orchestration.go import run_go
from nexus.registry.checkpoints import list_checkpoints
from nexus.registry.runs import get_run


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


VALID_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "parallelism": 1,
    "summary": "Do a small fix.",
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


PHASE3_MISSION_TEXT = """Faça uma auditoria FINAL, estritamente READ-ONLY, do E2E da Phase 3 do Nexus.

REPOSITÓRIO:
C:\\Users\\Vitor Sanches\\Desktop\\Devops\\nexus

RUN A AUDITAR:
RUN-16B9AA70

NÃO altere arquivos.
NÃO faça commit.
NÃO faça push.

Objetivo:

Determinar com evidência persistida se a propagação real:

Worker 0 --> DEPENDENCY CONTEXT --> Worker 2
Worker 1 --^

funcionou corretamente.

AUDITE:

1. PLANO

Confirme que o plano aprovado contém exatamente:

Worker 0:
depends_on=[]

Worker 1:
depends_on=[]

Worker 2:
depends_on=[0,1]

Leia nexus/orchestration/dag.py e nexus/orchestration/context.py.
"""


# ---------------------------------------------------------------------------
# 1-6: pure classifier tests -- no database, no run_go, no mocks.
# ---------------------------------------------------------------------------
class ClassifyMissionScopeTestCase(unittest.TestCase):
    # -- 1. determinism ------------------------------------------------
    def test_same_input_yields_same_classification_every_time(self):
        text = "Analise este projeto e identifique os principais problemas."

        results = [classify_mission_scope(text) for _ in range(25)]

        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0].scope, MissionScope.BROAD)

    def test_determinism_holds_for_a_bounded_mission_too(self):
        text = "Revise nexus/routing/service.py para verificar o fallback mechanical LOW."

        results = [classify_mission_scope(text) for _ in range(25)]

        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0].scope, MissionScope.BOUNDED)

    # -- 2. empty / whitespace ------------------------------------------
    def test_empty_string_does_not_crash_and_is_bounded(self):
        result = classify_mission_scope("")

        self.assertEqual(result.scope, MissionScope.BOUNDED)
        self.assertIn("EMPTY_INPUT", result.reason_codes)

    def test_whitespace_only_does_not_crash_and_is_bounded(self):
        result = classify_mission_scope("   \n\t  ")

        self.assertEqual(result.scope, MissionScope.BOUNDED)
        self.assertIn("EMPTY_INPUT", result.reason_codes)

    def test_none_input_does_not_crash_and_is_bounded(self):
        # classify_mission_scope's contract only documents str input, but
        # defensive handling of a non-str value must not raise -- a
        # future caller passing None through by mistake must fail safe
        # (BOUNDED = no behavior change), never crash the Run.
        result = classify_mission_scope(None)  # type: ignore[arg-type]

        self.assertEqual(result.scope, MissionScope.BOUNDED)

    # -- 3. bounded examples ----------------------------------------------
    def test_bounded_read_file_and_identify_function(self):
        result = classify_mission_scope(
            "Read nexus/orchestration/dag.py and identify the sink_nodes "
            "function."
        )
        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_bounded_fix_specific_calculation_in_named_file_pt(self):
        result = classify_mission_scope(
            "Corrija o cálculo de sink nodes em nexus/orchestration/dag.py."
        )
        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_bounded_run_routing_tests_and_report_failures_pt(self):
        result = classify_mission_scope(
            "Execute os testes de routing e informe quais falharam."
        )
        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_bounded_review_named_file_for_specific_fallback_pt(self):
        result = classify_mission_scope(
            "Revise nexus/routing/service.py para verificar o fallback "
            "mechanical LOW."
        )
        self.assertEqual(result.scope, MissionScope.BOUNDED)

    # -- 4. broad examples --------------------------------------------------
    def test_broad_analyze_this_project_main_problems_pt(self):
        result = classify_mission_scope(
            "Analise este projeto e identifique os principais problemas."
        )
        self.assertEqual(result.scope, MissionScope.BROAD)

    def test_broad_analyze_architecture_propose_priority_improvements_pt(self):
        result = classify_mission_scope(
            "Analise a arquitetura do Nexus e proponha as melhorias "
            "prioritárias."
        )
        self.assertEqual(result.scope, MissionScope.BROAD)

    def test_broad_find_improvement_opportunities_in_repo_pt(self):
        result = classify_mission_scope(
            "Encontre oportunidades de melhoria neste repositório."
        )
        self.assertEqual(result.scope, MissionScope.BROAD)

    def test_broad_review_repo_identify_issues_propose_priorities_en(self):
        result = classify_mission_scope(
            "Review this repository, identify the most important "
            "architectural and reliability issues, and propose "
            "priorities."
        )
        self.assertEqual(result.scope, MissionScope.BROAD)

    # -- 5. conservative ambiguity --------------------------------------
    def test_ambiguous_broad_scope_anchor_alone_stays_bounded(self):
        # Only the undelimited-scope anchor is present; no open-ended
        # investigative target. A single broad-sounding signal must not
        # tip the classification.
        result = classify_mission_scope("Review this repository.")

        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_ambiguous_open_ended_target_alone_stays_bounded(self):
        # Only the open-ended target phrase is present; no scope anchor
        # naming a project/repository/architecture as a whole.
        result = classify_mission_scope("Identify the most important issues.")

        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_ambiguous_generic_architecture_mention_stays_bounded(self):
        result = classify_mission_scope("Analyze the architecture.")

        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_ambiguous_short_vague_request_stays_bounded(self):
        result = classify_mission_scope("Melhore o sistema.")

        self.assertEqual(result.scope, MissionScope.BOUNDED)

    # -- 6. Phase 3 regression --------------------------------------------
    def test_phase3_real_mission_text_is_bounded(self):
        # The exact class of mission text validated E2E in RUN-16B9AA70:
        # explicit Worker 0/1/2, explicit depends_on, explicit target
        # files. Must never classify BROAD -- Phase 4.1 must not change
        # behavior for the mission shape Phase 3 already proved works.
        result = classify_mission_scope(PHASE3_MISSION_TEXT)

        self.assertEqual(result.scope, MissionScope.BOUNDED)


# ---------------------------------------------------------------------------
# 7-8: integration with the live nexus go path (run_go), using a disposable
# temp database exactly like tests/test_checkpoint_registry.py.
# ---------------------------------------------------------------------------
class MissionTriageIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(json.dumps(FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def _run_go_with_mocked_manager_and_worker(self, request_text: str) -> dict:
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_PLAN,
            },
        ), patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
            },
        ), patch(
            "nexus.orchestration.go.run_scout_stage",
            return_value="[Phase 4.2B scout evidence — mocked for Phase 4.1 regression]",
), patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=[{
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
                "_planned_scope": "Fix the thing.",
            }],
        ):
            return run_go(request_text, project_query="norte")

    # -- 7. no execution-behavior change ---------------------------------
    def test_bounded_mission_still_reaches_run_manager_and_completes(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_PLAN,
            },
        ) as mock_run_manager, patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
            },
), patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=[{
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
                "_planned_scope": "Fix the thing.",
            }],
        ):
            result = run_go("Norte: fix a bug", project_query="norte")

        mock_run_manager.assert_called_once()
        self.assertEqual(get_run(result["run_id"])["status"], "COMPLETED")

    def test_broad_mission_still_reaches_run_manager_and_completes(self):
        # Phase 4.2B note: BROAD missions now run a scout stage before the
        # Manager call. This test mocks run_scout_stage so the Manager is
        # still called exactly once and the run completes normally.
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_PLAN,
            },
        ) as mock_run_manager, patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
            },
        ), patch(
            "nexus.orchestration.go.run_scout_stage",
            return_value="[Phase 4.2B scout evidence — mocked]",
), patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=[{
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
                "_planned_scope": "Fix the thing.",
            }],
        ):
            result = run_go(
                "Norte: Analise este projeto e identifique os principais "
                "problemas.",
                project_query="norte",
            )

        mock_run_manager.assert_called_once()
        self.assertEqual(get_run(result["run_id"])["status"], "COMPLETED")

    def test_bounded_and_broad_missions_produce_identical_plan_and_worker_count(
        self,
    ):
        # Belt-and-suspenders: both classifications must yield a Run
        # whose approved plan / worker count came from the exact same
        # (mocked) run_manager output -- i.e. nothing about the plan
        # itself was altered based on triage classification in this
        # phase.
        bounded_result = self._run_go_with_mocked_manager_and_worker(
            "Norte: fix a bug"
        )
        broad_result = self._run_go_with_mocked_manager_and_worker(
            "Norte: Analise este projeto e identifique os principais "
            "problemas."
        )

        self.assertEqual(
            len(bounded_result["plan"]["workers"]),
            len(broad_result["plan"]["workers"]),
        )
        self.assertEqual(
            bounded_result["plan"]["workers"][0]["route_class"],
            broad_result["plan"]["workers"][0]["route_class"],
        )

    # -- 8. checkpoint persistence ----------------------------------------
    def test_mission_triage_checkpoint_recorded_once_with_bounded(self):
        result = self._run_go_with_mocked_manager_and_worker("Norte: fix a bug")

        checkpoints = list_checkpoints(result["run_id"])
        triage_checkpoints = [
            c for c in checkpoints if c["boundary"] == "MISSION_TRIAGE"
        ]

        self.assertEqual(len(triage_checkpoints), 1)
        self.assertEqual(
            triage_checkpoints[0]["payload"]["classification"], "BOUNDED"
        )

    def test_mission_triage_checkpoint_recorded_once_with_broad(self):
        result = self._run_go_with_mocked_manager_and_worker(
            "Norte: Analise este projeto e identifique os principais "
            "problemas."
        )

        checkpoints = list_checkpoints(result["run_id"])
        triage_checkpoints = [
            c for c in checkpoints if c["boundary"] == "MISSION_TRIAGE"
        ]

        self.assertEqual(len(triage_checkpoints), 1)
        self.assertEqual(
            triage_checkpoints[0]["payload"]["classification"], "BROAD"
        )

    def test_mission_triage_checkpoint_is_the_first_checkpoint_recorded(self):
        # Triage must happen at the logical start of the mission, before
        # PLAN_APPROVED -- confirming the integration point matches the
        # approved Phase 4.1 design (before plan generation, not after).
        result = self._run_go_with_mocked_manager_and_worker("Norte: fix a bug")

        checkpoints = list_checkpoints(result["run_id"])

        self.assertGreaterEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0]["boundary"], "MISSION_TRIAGE")
        boundaries = [c["boundary"] for c in checkpoints]
        self.assertIn("PLAN_APPROVED", boundaries)
        self.assertLess(
            boundaries.index("MISSION_TRIAGE"),
            boundaries.index("PLAN_APPROVED"),
        )


if __name__ == "__main__":
    unittest.main()
