"""Tests for the Phase C Nexus Interface MVP web layer and go_runner.

Groups:
- GoRunnerStateTest     : pure-Python go_runner unit tests (no HTTP needed)
- ProjectsServiceTest   : direct calls to projects registry (no HTTP needed)
- CheckpointsServiceTest: direct calls to checkpoints registry (no HTTP needed)
- ProjectsEndpointTest  : GET /api/projects  (skipped if starlette absent)
- CheckpointsEndpointTest: GET /api/runs/{id}/checkpoints (skipped if absent)
- GoEndpointTest        : POST /api/go, GET /api/go/jobs/* (skipped if absent)
- NexusPageTest         : GET /nexus, static assets (skipped if absent)
- NexusTemplateTest     : template/CSS/JS file content checks (always runs)
"""

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client():
    """Build a Starlette TestClient. Raises ImportError if not available."""
    from starlette.testclient import TestClient
    from nexus.web.app import create_app
    return TestClient(
        create_app(), base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000), raise_server_exceptions=True,
    )


# ---------------------------------------------------------------------------
# go_runner unit tests  (always run)
# ---------------------------------------------------------------------------


class GoRunnerStateTest(unittest.TestCase):
    """go_runner module internal state management."""

    # Saved before any test runs; lets setUp/tearDown reliably restore
    # _worker even if a concurrent patch leaks and corrupts the attribute.
    _real_worker = None

    def setUp(self):
        import inspect
        from nexus.web import go_runner
        # Capture the genuine _worker function the very first time.
        if GoRunnerStateTest._real_worker is None and inspect.isfunction(go_runner._worker):
            GoRunnerStateTest._real_worker = go_runner._worker
        # Restore if a previous test leaked a Mock into the module attribute.
        if GoRunnerStateTest._real_worker is not None:
            go_runner._worker = GoRunnerStateTest._real_worker
        go_runner._jobs.clear()

    def tearDown(self):
        from nexus.web import go_runner
        if GoRunnerStateTest._real_worker is not None:
            go_runner._worker = GoRunnerStateTest._real_worker

    def test_launch_go_returns_job_id(self):
        """launch_go() returns a JOB- prefixed ID without blocking."""
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None):
            job_id = go_runner.launch_go("test mission", None)
        self.assertRegex(job_id, r"^JOB-[0-9A-F]{8}$")

    def test_get_job_returns_state(self):
        """get_job() returns job dict after launch."""
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None):
            job_id = go_runner.launch_go("test mission", "proj-1")
        time.sleep(0.05)
        job = go_runner.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["project_id"], "proj-1")
        self.assertIn("started_at", job)

    def test_get_job_none_for_unknown(self):
        """get_job() returns None for a non-existent job_id."""
        from nexus.web import go_runner
        self.assertIsNone(go_runner.get_job("JOB-00000000"))

    def test_list_jobs_returns_list(self):
        """list_jobs() returns all launched jobs."""
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None):
            go_runner.launch_go("mission A", None)
            go_runner.launch_go("mission B", None)
        jobs = go_runner.list_jobs()
        self.assertGreaterEqual(len(jobs), 2)

    def test_worker_sets_completed_on_success(self):
        """_worker() marks job completed with run_id on go success."""
        from nexus.web import go_runner
        job_id = "JOB-TESTABCD"
        go_runner._jobs[job_id] = {
            "job_id": job_id, "status": "running", "run_id": None,
            "error": None, "error_code": None,
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
            "project_id": None, "mission_text": "test",
        }
        mock_result = {"run_id": "RUN-ABCDEF12", "status": "COMPLETED"}
        with patch("nexus.orchestration.go.run_go", return_value=mock_result):
            go_runner._worker(job_id, "test mission", None)
        job = go_runner._jobs[job_id]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["run_id"], "RUN-ABCDEF12")
        self.assertIsNotNone(job["finished_at"])

    def test_worker_sets_failed_on_go_error(self):
        """_worker() marks job failed on GoError, preserving run_id."""
        from nexus.web import go_runner
        job_id = "JOB-FAILTEST"
        go_runner._jobs[job_id] = {
            "job_id": job_id, "status": "running", "run_id": None,
            "error": None, "error_code": None,
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
            "project_id": None, "mission_text": "test",
        }
        from nexus.orchestration.go import GoError
        err = GoError("PROJECT_NOT_FOUND", "project missing", run_id="RUN-FAILXXX")
        with patch("nexus.orchestration.go.run_go", side_effect=err):
            go_runner._worker(job_id, "test mission", None)
        job = go_runner._jobs[job_id]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["run_id"], "RUN-FAILXXX")
        self.assertEqual(job["error_code"], "PROJECT_NOT_FOUND")
        self.assertIsNotNone(job["finished_at"])

    def test_non_reasoning_failure_recorded_as_error_code(self):
        """Non-reasoning failures are recorded by error_code; go_runner never touches effort."""
        from nexus.web import go_runner
        job_id = "JOB-EFFRTEST"
        go_runner._jobs[job_id] = {
            "job_id": job_id, "status": "running", "run_id": None,
            "error": None, "error_code": None,
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
            "project_id": None, "mission_text": "test",
        }
        from nexus.orchestration.go import GoError
        err = GoError("PROVIDER_FAILURE", "provider down", run_id="RUN-PROVTEST")
        with patch("nexus.orchestration.go.run_go", side_effect=err):
            go_runner._worker(job_id, "test mission", None)
        job = go_runner._jobs[job_id]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "PROVIDER_FAILURE")

    def test_concurrent_launch_creates_independent_jobs(self):
        """Two concurrent launches produce distinct job IDs."""
        from nexus.web import go_runner
        ids = []
        ids_lock = threading.Lock()
        # Patch _worker once in the main thread (thread-safe).  Both spawned
        # threads see the mock via the module attribute; no concurrent
        # patch.object calls that would race on save/restore.
        with patch.object(go_runner, "_worker", return_value=None):
            def launch():
                jid = go_runner.launch_go("parallel mission", None)
                with ids_lock:
                    ids.append(jid)
            t1 = threading.Thread(target=launch)
            t2 = threading.Thread(target=launch)
            t1.start(); t2.start()
            t1.join(); t2.join()
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_job_mission_text_truncated_at_200(self):
        """mission_text stored in job is at most 200 chars."""
        from nexus.web import go_runner
        long_text = "X" * 300
        with patch.object(go_runner, "_worker", return_value=None):
            job_id = go_runner.launch_go(long_text, None)
        job = go_runner.get_job(job_id)
        self.assertLessEqual(len(job["mission_text"]), 200)


# ---------------------------------------------------------------------------
# Projects and checkpoints registry (direct, no HTTP)
# ---------------------------------------------------------------------------


class ProjectsServiceTest(unittest.TestCase):
    """Direct calls to list_projects() service function."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import nexus.registry.database as _db
        import nexus.registry.projects as _proj
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        self._orig_db   = _db.DATABASE_PATH
        self._orig_stor = _db.STORAGE_DIR
        self._orig_pf   = _proj.PROJECTS_FILE
        _db.STORAGE_DIR   = tmp
        _db.DATABASE_PATH = tmp / "nexus-test.db"
        _proj.PROJECTS_FILE = tmp / "projects.json"
        import json
        (tmp / "projects.json").write_text(json.dumps({"projects": [
            {"id": "test-proj", "name": "Test Project",
             "path": "C:\\fake", "aliases": [], "enabled": True}
        ]}))
        _db.initialize_database()
        _proj.sync_projects()

    def tearDown(self):
        import nexus.registry.database as _db
        import nexus.registry.projects as _proj
        _db.DATABASE_PATH  = self._orig_db
        _db.STORAGE_DIR    = self._orig_stor
        _proj.PROJECTS_FILE = self._orig_pf
        self._tmp.cleanup()

    def test_list_projects_returns_list(self):
        """list_projects() always returns a list (possibly empty)."""
        from nexus.registry.projects import list_projects
        result = list_projects()
        self.assertIsInstance(result, list)

    def test_project_rows_have_required_fields(self):
        """Each project row has id, name, path, enabled."""
        from nexus.registry.projects import list_projects
        for row in list_projects():
            d = dict(row)
            self.assertIn("id", d)
            self.assertIn("name", d)
            self.assertIn("path", d)
            self.assertIn("enabled", d)


class CheckpointsServiceTest(unittest.TestCase):
    """Direct calls to checkpoint registry service functions."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import nexus.registry.database as _db
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        self._orig_db   = _db.DATABASE_PATH
        self._orig_stor = _db.STORAGE_DIR
        _db.STORAGE_DIR   = tmp
        _db.DATABASE_PATH = tmp / "nexus-test.db"
        _db.initialize_database()

    def tearDown(self):
        import nexus.registry.database as _db
        _db.DATABASE_PATH = self._orig_db
        _db.STORAGE_DIR   = self._orig_stor
        self._tmp.cleanup()

    def test_list_checkpoints_empty_for_unknown_run(self):
        """list_checkpoints() returns [] for a run that has no checkpoints."""
        from nexus.registry.checkpoints import list_checkpoints
        self.assertEqual(list_checkpoints("RUN-NOTHERE"), [])

    def test_list_checkpoints_returns_ordered_entries(self):
        """Checkpoints are returned in ascending sequence order."""
        from nexus.registry.checkpoints import list_checkpoints, record_checkpoint
        from nexus.registry.runs import create_run
        import nexus.registry.projects as _proj
        import json
        from pathlib import Path
        import nexus.registry.database as _db
        pfile = Path(str(_db.STORAGE_DIR)) / "projects.json"
        pfile.write_text(json.dumps({"projects": [
            {"id": "tp2", "name": "TP2", "path": "C:\\fake2", "aliases": [], "enabled": True}
        ]}))
        _proj.PROJECTS_FILE = pfile
        _proj.sync_projects()
        run_id = create_run(project_id="tp2", input_text="test order")
        record_checkpoint(run_id, "MISSION_TRIAGE", {"status": "ROUTING"})
        record_checkpoint(run_id, "PLAN_APPROVED",  {"status": "RUNNING"})
        result = list_checkpoints(run_id)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["boundary"], "MISSION_TRIAGE")
        self.assertEqual(result[1]["boundary"], "PLAN_APPROVED")
        self.assertLess(result[0]["sequence"], result[1]["sequence"])

    def test_checkpoint_payload_round_trips(self):
        """Payload dict is preserved exactly on write/read."""
        from nexus.registry.checkpoints import get_latest_checkpoint, record_checkpoint
        from nexus.registry.runs import create_run
        import nexus.registry.projects as _proj
        import json
        from pathlib import Path
        import nexus.registry.database as _db
        pfile = Path(str(_db.STORAGE_DIR)) / "projects.json"
        pfile.write_text(json.dumps({"projects": [
            {"id": "tp3", "name": "TP3", "path": "C:\\fake3", "aliases": [], "enabled": True}
        ]}))
        _proj.PROJECTS_FILE = pfile
        _proj.sync_projects()
        run_id = create_run(project_id="tp3", input_text="payload test")
        payload = {"status": "RUNNING", "worker_count": 3, "risk": "HIGH"}
        record_checkpoint(run_id, "EXECUTION_START", payload)
        cp = get_latest_checkpoint(run_id)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["payload"]["status"], "RUNNING")
        self.assertEqual(cp["payload"]["worker_count"], 3)
        self.assertEqual(cp["payload"]["risk"], "HIGH")

    def test_get_approved_plan_returns_none_for_unknown_run(self):
        """get_approved_plan() is None for a run with no persisted plan."""
        from nexus.registry.checkpoints import get_approved_plan
        self.assertIsNone(get_approved_plan("RUN-NOPLAN01"))


# ---------------------------------------------------------------------------
# Template / static asset content checks  (always run, no HTTP)
# ---------------------------------------------------------------------------


class NexusTemplateTest(unittest.TestCase):
    """Nexus HTML template and static files contain required elements."""

    WEB_DIR = Path(__file__).resolve().parents[1] / "nexus" / "web"

    def _read(self, *parts):
        return (self.WEB_DIR / Path(*parts)).read_text(encoding="utf-8")

    def test_nexus_html_exists(self):
        self.assertTrue((self.WEB_DIR / "templates" / "nexus.html").exists())

    def test_nexus_css_exists(self):
        self.assertTrue((self.WEB_DIR / "static" / "css" / "nexus.css").exists())

    def test_nexus_js_exists(self):
        self.assertTrue((self.WEB_DIR / "static" / "js" / "nexus.js").exists())

    def test_go_runner_module_exists(self):
        self.assertTrue((self.WEB_DIR / "go_runner.py").exists())

    def test_template_has_mission_input(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("mission-input", html)

    def test_template_has_run_button(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("RUN MISSION", html)

    def test_template_has_all_lifecycle_stages(self):
        html = self._read("templates", "nexus.html")
        for stage in ["lc-manager", "lc-scouts", "lc-workers", "lc-review", "lc-integration"]:
            self.assertIn(stage, html, f"Missing lifecycle stage: {stage}")

    def test_template_has_sidebar_projects(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("sidebar-projects", html)

    def test_template_has_sidebar_runs(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("sidebar-runs", html)

    def test_template_has_manager_model_selector(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("manager-model-select", html)
        self.assertIn("manager-model-filter", html)

    def test_template_has_run_header_manager_badge(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("ws-run-manager", html)

    def test_template_references_nexus_css(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("nexus.css", html)

    def test_template_references_nexus_js(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("nexus.js", html)

    def test_template_has_agent_grid(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("agent-grid", html)

    def test_template_has_timeline(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("timeline", html)

    def test_template_has_project_select(self):
        html = self._read("templates", "nexus.html")
        self.assertIn("project-select", html)

    def test_css_has_dark_background(self):
        css = self._read("static", "css", "nexus.css")
        self.assertIn("--bg", css)
        self.assertIn("#0d1117", css)  # dark theme base color

    def test_css_has_lifecycle_stage_styles(self):
        css = self._read("static", "css", "nexus.css")
        self.assertIn("lifecycle-stage", css)
        self.assertIn(".lifecycle-stage.done", css)
        self.assertIn(".lifecycle-stage.active", css)

    def test_css_has_agent_card_styles(self):
        css = self._read("static", "css", "nexus.css")
        self.assertIn(".agent-card", css)
        self.assertIn(".agent-role", css)
        self.assertIn(".agent-status", css)

    def test_css_has_timeline_styles(self):
        css = self._read("static", "css", "nexus.css")
        self.assertIn(".tl-item", css)
        self.assertIn(".tl-boundary", css)

    def test_js_polls_operational_runs_endpoint(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("/api/operational/runs", js)

    def test_js_polls_checkpoints_endpoint(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("/checkpoints", js)

    def test_js_posts_to_go_endpoint(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("/api/go", js)

    def test_js_polls_jobs_endpoint(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("/api/go/jobs/", js)

    def test_js_has_submit_mission_function(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("submitMission", js)

    def test_js_has_agent_card_rendering(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("renderAgentCard", js)

    def test_js_has_timeline_rendering(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("renderTimeline", js)

    def test_js_has_lifecycle_update(self):
        js = self._read("static", "js", "nexus.js")
        self.assertIn("updateLifecycle", js)


# ---------------------------------------------------------------------------
# HTTP endpoint tests  (skipped when starlette/fastapi not installed)
# ---------------------------------------------------------------------------


def _skip_if_no_testclient(test_method):
    """Decorator: skip the test if starlette TestClient is unavailable."""
    import functools
    @functools.wraps(test_method)
    def wrapper(self, *args, **kwargs):
        try:
            from starlette.testclient import TestClient  # noqa: F401
        except ImportError:
            self.skipTest("starlette TestClient not installed")
        return test_method(self, *args, **kwargs)
    return wrapper


class ProjectsEndpointTest(unittest.TestCase):
    """GET /api/projects"""

    def setUp(self):
        projects_mock = patch("nexus.registry.projects.list_projects", return_value=[])
        projects_mock.start()
        self.addCleanup(projects_mock.stop)
        try:
            self.client = _make_client()
        except ImportError:
            self.client = None

    @_skip_if_no_testclient
    def test_projects_returns_200(self):
        r = self.client.get("/api/projects")
        self.assertEqual(r.status_code, 200)

    @_skip_if_no_testclient
    def test_projects_has_projects_key(self):
        r = self.client.get("/api/projects")
        self.assertIn("projects", r.json())

    @_skip_if_no_testclient
    def test_projects_list_is_list(self):
        r = self.client.get("/api/projects")
        self.assertIsInstance(r.json()["projects"], list)


class CheckpointsEndpointTest(unittest.TestCase):
    """GET /api/runs/{run_id}/checkpoints"""

    def setUp(self):
        try:
            self.client = _make_client()
        except ImportError:
            self.client = None

    @_skip_if_no_testclient
    def test_checkpoints_unknown_run_returns_200(self):
        r = self.client.get("/api/runs/RUN-NONEXISTENT/checkpoints")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["checkpoints"], [])
        self.assertIsNone(data["approved_plan"])

    @_skip_if_no_testclient
    def test_checkpoints_with_real_data(self):
        from nexus.registry.database import get_connection, initialize_database
        from nexus.registry.checkpoints import record_checkpoint
        from nexus.registry.runs import create_run
        initialize_database()
        with get_connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO projects (id, name, path) VALUES (?, ?, ?)",
                ("tp-http", "HTTP checkpoint test", "C:/fake"),
            )
        run_id = create_run(project_id="tp-http", input_text="checkpoint endpoint")
        record_checkpoint(run_id, "MISSION_TRIAGE", {"status": "ROUTING"})
        record_checkpoint(run_id, "PLAN_APPROVED",  {"status": "RUNNING"})
        r = self.client.get(f"/api/runs/{run_id}/checkpoints")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["checkpoints"]), 2)
        self.assertEqual(data["checkpoints"][0]["boundary"], "MISSION_TRIAGE")


class GoEndpointTest(unittest.TestCase):
    """POST /api/go and GET /api/go/jobs/*"""

    def setUp(self):
        from nexus.web import go_runner
        go_runner._jobs.clear()
        try:
            self.client = _make_client()
        except ImportError:
            self.client = None

    @_skip_if_no_testclient
    def test_go_empty_mission_is_422(self):
        r = self.client.post("/api/go", json={"mission": ""})
        self.assertEqual(r.status_code, 422)

    @_skip_if_no_testclient
    def test_go_returns_job_id(self):
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None):
            r = self.client.post("/api/go", json={"mission": "Test mission"})
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.json()["job_id"], r"^JOB-[0-9A-F]{8}$")

    @_skip_if_no_testclient
    def test_go_job_poll_200(self):
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None):
            r_post = self.client.post("/api/go", json={"mission": "Polling test"})
        job_id = r_post.json()["job_id"]
        r_poll = self.client.get(f"/api/go/jobs/{job_id}")
        self.assertEqual(r_poll.status_code, 200)
        self.assertIn("job", r_poll.json())

    @_skip_if_no_testclient
    def test_go_job_unknown_is_404(self):
        r = self.client.get("/api/go/jobs/JOB-BADID00")
        self.assertEqual(r.status_code, 404)

    @_skip_if_no_testclient
    def test_go_jobs_list_200(self):
        r = self.client.get("/api/go/jobs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("jobs", r.json())

    @_skip_if_no_testclient
    def test_go_with_manager_model_reaches_worker(self):
        """A valid manager_model in the request body is threaded through to
        go_runner._worker (and therefore to run_go()), not dropped."""
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None) as mock_worker:
            r = self.client.post(
                "/api/go",
                json={"mission": "Test mission", "manager_model": "codex/gpt-5.6-sol"},
            )
        self.assertEqual(r.status_code, 200)
        mock_worker.assert_called_once()
        self.assertEqual(mock_worker.call_args.args[3], "codex/gpt-5.6-sol")

    @_skip_if_no_testclient
    def test_go_with_invalid_manager_model_is_400(self):
        """An ineligible manager_model is rejected before any job launches.

        cc/claude-sonnet-5-high is a real, approved catalog route (used by
        Reviewer), but has no "planning" capability -- being valid for one
        role does not make it valid for Manager.
        """
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None) as mock_worker:
            r = self.client.post(
                "/api/go",
                json={"mission": "Test mission", "manager_model": "cc/claude-sonnet-5-high"},
            )
        self.assertEqual(r.status_code, 400)
        mock_worker.assert_not_called()

    @_skip_if_no_testclient
    def test_go_without_manager_model_defaults_to_none(self):
        """Omitting manager_model preserves the existing automatic behavior."""
        from nexus.web import go_runner
        with patch.object(go_runner, "_worker", return_value=None) as mock_worker:
            r = self.client.post("/api/go", json={"mission": "Test mission"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(mock_worker.call_args.args[3])


class ManagerModelsEndpointTest(unittest.TestCase):
    """GET /api/manager/models -- backs the New Mission composer selector."""

    def setUp(self):
        try:
            self.client = _make_client()
        except ImportError:
            self.client = None

    @_skip_if_no_testclient
    def test_manager_models_200(self):
        r = self.client.get("/api/manager/models")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("auto", data)
        self.assertIn("selectable", data)
        self.assertIn("ineligible", data)

    @_skip_if_no_testclient
    def test_manager_models_selectable_are_known_codex_models(self):
        from nexus.policies.manager_model import MANAGER_MODEL_CHOICES
        r = self.client.get("/api/manager/models")
        ids = {m["model_id"] for m in r.json()["selectable"]}
        self.assertEqual(ids, set(MANAGER_MODEL_CHOICES))


class NexusPageTest(unittest.TestCase):
    """GET /nexus – page loads correctly."""

    def setUp(self):
        try:
            self.client = _make_client()
        except ImportError:
            self.client = None

    @_skip_if_no_testclient
    def test_nexus_page_200(self):
        r = self.client.get("/nexus")
        self.assertEqual(r.status_code, 200)

    @_skip_if_no_testclient
    def test_nexus_page_is_html(self):
        r = self.client.get("/nexus")
        self.assertIn("text/html", r.headers.get("content-type", ""))

    @_skip_if_no_testclient
    def test_nexus_page_has_mission_commander(self):
        r = self.client.get("/nexus")
        self.assertIn("mission-input", r.text)

    @_skip_if_no_testclient
    def test_nexus_page_has_run_button(self):
        r = self.client.get("/nexus")
        self.assertIn("RUN MISSION", r.text)


if __name__ == "__main__":
    unittest.main()
