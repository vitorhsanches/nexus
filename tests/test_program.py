import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus import failure_classification as fc
from nexus.orchestration.go import GoError
from nexus.program import acceptance, architect, bootstrap, runner, state, verify
from nexus.process_timeout import ProcessWatchdog


def _spec(phases=2, acceptance_criteria=None):
    return {
        "summary": "A todo app.",
        "stack": "Python",
        "architecture": "single module",
        "setup_commands": [],
        "final_verify_commands": ["python -c \"print('final')\""],
        "acceptance_criteria": (
            acceptance_criteria
            if acceptance_criteria is not None
            else [
                {
                    "id": "AC-1",
                    "description": "Requirements file exists.",
                    "verification_type": "file_exists",
                    "required": True,
                    "path": "REQUIREMENTS.md",
                }
            ]
        ),
        "phases": [
            {
                "title": f"Phase {n}",
                "goal": f"goal {n}",
                "acceptance_criteria": f"criteria {n}",
                "verify_commands": [f"python -c \"print({n})\""],
                "criteria": [],
            }
            for n in range(1, phases + 1)
        ],
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    projects_file = tmp_path / "projects.json"
    projects_file.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(bootstrap, "LOCAL_PROJECTS_FILE", projects_file)
    monkeypatch.setattr(bootstrap, "load_project_entries", lambda: json.loads(projects_file.read_text(encoding="utf-8"))["projects"])
    monkeypatch.setattr(bootstrap, "sync_projects", lambda: None)
    monkeypatch.setattr(state, "PROGRAMS_DIR", tmp_path / "programs")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"workspace": workspace, "projects_file": projects_file}


# --- bootstrap -------------------------------------------------------------


def test_slugify_strips_accents_and_symbols():
    assert bootstrap.slugify("Gestão Financeira 2.0!") == "gestao-financeira-2-0"
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.slugify("!!!")


def test_bootstrap_creates_committed_repo_and_registers(isolated):
    project = bootstrap.bootstrap_project(
        "Meu App", "Build a todo app.", isolated["workspace"]
    )

    repo = isolated["workspace"] / "meu-app"
    assert project["project_id"] == "meu-app"
    assert "Build a todo app." in (repo / "REQUIREMENTS.md").read_text(encoding="utf-8")
    assert "node_modules/" in (repo / ".gitignore").read_text(encoding="utf-8")
    head = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head == project["initial_commit"]
    assert bootstrap.git(["status", "--porcelain"], repo).stdout == ""

    registered = json.loads(isolated["projects_file"].read_text(encoding="utf-8"))
    assert registered["projects"][0]["id"] == "meu-app"
    assert registered["projects"][0]["path"] == str(repo)


def test_bootstrap_refuses_non_empty_dir_and_duplicate_id(isolated):
    (isolated["workspace"] / "taken").mkdir()
    (isolated["workspace"] / "taken" / "file.txt").write_text("x")
    with pytest.raises(bootstrap.BootstrapError, match="not empty"):
        bootstrap.bootstrap_project("taken", "reqs", isolated["workspace"])

    bootstrap.bootstrap_project("dup", "reqs", isolated["workspace"])
    other = isolated["workspace"] / "elsewhere"
    with pytest.raises(bootstrap.BootstrapError, match="already registered"):
        bootstrap.bootstrap_project("dup", "reqs", other)


def test_bootstrap_rejects_empty_requirements(isolated):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.bootstrap_project("x", "   ", isolated["workspace"])


# --- architect -------------------------------------------------------------


def test_extract_spec_takes_last_valid_envelope():
    echoed_example = "NEXUS_SPEC_BEGIN {\"summary\": \"\"} NEXUS_SPEC_END"
    real = "NEXUS_SPEC_BEGIN " + json.dumps(_spec()) + " NEXUS_SPEC_END"
    spec = architect.extract_spec(echoed_example + "\n...\n" + real)
    assert len(spec["phases"]) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.pop("summary"),
        lambda s: s.update(final_verify_commands=[]),
        lambda s: s.update(phases=[]),
        lambda s: s["phases"][0].update(verify_commands=[]),
        lambda s: s["phases"][0].update(verify_commands=["ok", ""]),
        lambda s: s["phases"][0].pop("goal"),
        lambda s: s.update(phases=_spec(architect.MAX_PHASES + 1)["phases"]),
        lambda s: s.update(acceptance_criteria=[]),
        lambda s: s["acceptance_criteria"][0].update(verification_type="bogus"),
        lambda s: s["phases"][0].update(
            criteria=[{**s["acceptance_criteria"][0]}]  # duplicate id AC-1
        ),
        lambda s: s["acceptance_criteria"][0].update(path="../escape.txt"),
    ],
)
def test_validate_spec_fails_closed(mutate):
    spec = _spec()
    mutate(spec)
    with pytest.raises(ValueError):
        architect.validate_spec(spec)


def test_extract_spec_without_envelope_raises():
    with pytest.raises(ValueError, match="envelope"):
        architect.extract_spec("no spec here")


# --- verify ----------------------------------------------------------------


def _committed_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    bootstrap.git(["init", "-b", "main"], repo)
    (repo / "a.txt").write_text("one", encoding="utf-8")
    bootstrap.commit_all(repo, "init")
    return repo


def test_verification_runs_in_worktree_and_leaves_primary_clean(tmp_path):
    repo = _committed_repo(tmp_path)
    write = f'"{sys.executable}" -c "open(\'generated.txt\',\'w\').write(\'x\')"'

    result = verify.run_verification(str(repo), [write], [f'"{sys.executable}" -c "print(1)"'])

    assert result["passed"] is True
    assert len(result["results"]) == 2
    assert not (repo / "generated.txt").exists()
    assert bootstrap.git(["status", "--porcelain"], repo).stdout == ""
    assert (verify.verify_worktree_path(str(repo)) / "generated.txt").exists()


def test_verification_follows_new_head_and_cleans_untracked(tmp_path):
    repo = _committed_repo(tmp_path)
    ok = f'"{sys.executable}" -c "print(1)"'
    verify.run_verification(str(repo), [], [ok])
    worktree = verify.verify_worktree_path(str(repo))
    (worktree / "stray.txt").write_text("x")

    (repo / "b.txt").write_text("two", encoding="utf-8")
    new_head = bootstrap.commit_all(repo, "second")
    result = verify.run_verification(str(repo), [], [ok])

    assert result["head"] == new_head
    assert (worktree / "b.txt").exists()
    assert not (worktree / "stray.txt").exists()


def test_verification_stops_at_first_failure_with_excerpt(tmp_path):
    repo = _committed_repo(tmp_path)
    fail = f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(3)"'
    never = f'"{sys.executable}" -c "print(2)"'

    result = verify.run_verification(str(repo), [], [fail, never])

    assert result["passed"] is False
    assert len(result["results"]) == 1
    excerpt = verify.failure_excerpt(result)
    assert "exit code 3" in excerpt and "boom" in excerpt


def test_verification_timeout(tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)
    monkeypatch.setenv("NEXUS_VERIFY_TIMEOUT_SECONDS", "1")
    slow = f'"{sys.executable}" -c "import time; time.sleep(30)"'

    result = verify.run_verification(str(repo), [], [slow])

    assert result["passed"] is False
    assert result["results"][0]["timed_out"] is True
    assert "timed out" in verify.failure_excerpt(result)


# --- watchdog --------------------------------------------------------------


def test_watchdog_kills_hung_process():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with ProcessWatchdog(process, 1) as watchdog:
        for _ in process.stdout:
            pass
        process.wait()
    assert watchdog.fired is True


def test_watchdog_does_not_fire_for_fast_process():
    process = subprocess.Popen(
        [sys.executable, "-c", "print(1)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with ProcessWatchdog(process, 30) as watchdog:
        process.communicate()
    assert watchdog.fired is False


# --- runner ----------------------------------------------------------------


class FakeGo:
    """Scripted run_go: each call pops the next outcome (None = success).

    An outcome is either a bare GoError code (str) or a ``(code, detail)``
    tuple when a test needs the structured detail a real
    REVIEW_BLOCKED/WORKER_EXECUTION_FAILED GoError carries.
    """

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.missions = []
        self.exclude_models_calls = []

    def __call__(
        self, mission, project_query=None, planning_required=False,
        exclude_models=frozenset(),
    ):
        assert planning_required is True
        self.missions.append(mission)
        self.exclude_models_calls.append(exclude_models)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        run_id = f"RUN-{len(self.missions)}"
        if outcome is not None:
            code, detail = outcome if isinstance(outcome, tuple) else (outcome, None)
            raise GoError(code, "failed", run_id=run_id, detail=detail)
        return {"run_id": run_id, "status": "COMPLETED"}


class FakeVerify:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, repo, setup, commands, revision=None):
        self.calls.append({"commands": list(commands), "revision": revision})
        passed = self.outcomes.pop(0) if self.outcomes else True
        return {
            "passed": passed,
            # Real path coverage: FakeGo never touches the filesystem, so
            # the bootstrapped repo's HEAD is always a real, valid
            # revision -- echo it back instead of a fabricated string, so
            # acceptance.evaluate_criteria (real git worktree ops) works
            # unmodified in these tests.
            "head": revision or "HEAD",
            "results": [
                {
                    "command": commands[0],
                    "exit_code": 0 if passed else 1,
                    "timed_out": False,
                    "passed": passed,
                    "output_tail": "" if passed else "AssertionError: expected 2",
                }
            ],
        }


@pytest.fixture
def program_env(isolated, monkeypatch):
    monkeypatch.setattr(runner, "run_architect", lambda repo: _spec())
    go = FakeGo()
    ver = FakeVerify()
    monkeypatch.setattr(runner, "run_go", go)
    monkeypatch.setattr(runner, "run_verification", ver)
    return {"go": go, "verify": ver, **isolated}


def test_program_happy_path(program_env):
    program = runner.start_program("Todo", "Build a todo app.", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    assert [p["status"] for p in program["phases"]] == ["DONE", "DONE"]
    assert len(program_env["go"].missions) == 2
    assert "Phase 1 of 2" in program_env["go"].missions[0]
    assert "Phase 2 of 2" in program_env["go"].missions[1]
    assert "already implemented" in program_env["go"].missions[1]
    # phase 1, phase 2, final
    assert len(program_env["verify"].calls) == 3
    assert all(call["revision"] for call in program_env["verify"].calls)

    repo = program_env["workspace"] / "todo"
    assert (repo / "docs" / "NEXUS_SPEC.md").exists()
    report = (repo / "FINAL_REPORT.md").read_text(encoding="utf-8")
    assert "RUN-1" in report and "PASS" in report
    assert bootstrap.git(["status", "--porcelain"], repo).stdout == ""
    assert state.load_program(program["program_id"])["status"] == "COMPLETED"


def test_program_fixes_after_failed_verification(program_env):
    program_env["verify"].outcomes = [False, True]

    program = runner.start_program("Fixy", "reqs", program_env["workspace"])

    missions = program_env["go"].missions
    assert program["status"] == "COMPLETED"
    assert "verification failed" in missions[1]
    assert "AssertionError: expected 2" in missions[1]
    assert program["phases"][0]["attempts"] == 2
    assert program["phases"][0]["run_ids"] == ["RUN-1", "RUN-2"]


def test_program_retries_failed_run_with_failure_context(program_env):
    program_env["go"].outcomes = ["REVIEW_BLOCKED"]

    program = runner.start_program("Retry", "reqs", program_env["workspace"])

    missions = program_env["go"].missions
    assert program["status"] == "COMPLETED"
    assert "previous attempt at this phase failed" in missions[1]
    assert "REVIEW_BLOCKED" in missions[1]
    # verification is skipped for the failed run
    assert len(program_env["verify"].calls) == 3


def test_program_blocks_after_budget_and_resumes(program_env):
    program_env["go"].outcomes = ["PLAN_INVALID"] * runner.MAX_ATTEMPTS_PER_STAGE

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Stuck", "reqs", program_env["workspace"])

    blocked = state.load_program(info.value.program_id)
    assert blocked["status"] == "BLOCKED"
    assert blocked["phases"][0]["status"] == "BLOCKED"
    assert "PLAN_INVALID" in blocked["blocked_reason"]

    program = runner.resume_program(info.value.program_id)
    assert program["status"] == "COMPLETED"
    assert [p["status"] for p in program["phases"]] == ["DONE", "DONE"]


def test_resume_skips_done_phases(program_env, monkeypatch):
    program_env["go"].outcomes = [None] + ["X"] * runner.MAX_ATTEMPTS_PER_STAGE
    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Half", "reqs", program_env["workspace"])

    before = len(program_env["go"].missions)
    runner.resume_program(info.value.program_id)

    resumed = program_env["go"].missions[before:]
    assert len(resumed) == 1
    assert "Phase 2 of 2" in resumed[0]


def test_final_stage_verifies_first_then_fixes(program_env):
    # phase1 ok, phase2 ok, final fails, final ok after fix
    program_env["verify"].outcomes = [True, True, False, True]

    program = runner.start_program("Final", "reqs", program_env["workspace"])

    missions = program_env["go"].missions
    assert program["status"] == "COMPLETED"
    assert len(missions) == 3
    assert "Final verification: verification failed" in missions[2]
    assert program["final"]["attempts"] == 1


def test_spec_only_stops_before_phases(program_env):
    program = runner.start_program("Spec", "reqs", program_env["workspace"], stop_after_spec=True)

    assert program["status"] == "SPECIFIED"
    assert program_env["go"].missions == []


def test_program_persists_parent_task_contract(program_env):
    contract = {
        "goal": "Create the product.",
        "task_type": "SOFTWARE_PROJECT",
        "execution_mode": "PROGRAM",
    }
    program = runner.start_program(
        "Contract",
        "reqs",
        program_env["workspace"],
        stop_after_spec=True,
        task_contract=contract,
    )
    assert program["task_contract"] == contract
    assert state.load_program(program["program_id"])["task_contract"] == contract


def test_program_local_autonomy_does_not_bypass_phase_run_policy(program_env):
    contract = {
        "task_type": "SOFTWARE_PROJECT",
        "execution_mode": "PROGRAM",
        "authority_resolution": {
            "result": "AUTHORIZED",
            "autonomy_profile": "LOCAL_AUTONOMOUS",
        },
    }
    program = runner.start_program(
        "Local Policy",
        "reqs",
        program_env["workspace"],
        task_contract=contract,
    )
    assert program["status"] == "COMPLETED"
    assert len(program_env["go"].missions) == 2


def test_resume_does_not_repeat_durably_verified_stage(program_env):
    program = runner.start_program(
        "Verified", "reqs", program_env["workspace"], stop_after_spec=True
    )
    # A real revision (the repo's actual HEAD): Phase 4.5's resume revision
    # guard fails closed on a fabricated/mismatched one, by design.
    real_head = bootstrap.git(["rev-parse", "HEAD"], Path(program["path"])).stdout.strip()
    phase = program["phases"][0]
    phase["status"] = "RUNNING"
    phase["last_verification"] = {"passed": True, "head": real_head, "results": []}
    phase["target_revision"] = real_head
    state.save_program(program)

    resumed = runner.resume_program(program["program_id"])

    assert resumed["phases"][0]["status"] == "DONE"
    assert all("Phase 1 of 2" not in mission for mission in program_env["go"].missions)


def test_verification_can_bind_to_explicit_revision(tmp_path):
    repo = _committed_repo(tmp_path)
    first = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "b.txt").write_text("two", encoding="utf-8")
    bootstrap.commit_all(repo, "second")
    command = subprocess.list2cmdline([
        sys.executable,
        "-c",
        "import pathlib,sys; sys.exit(pathlib.Path('b.txt').exists())",
    ])

    result = verify.run_verification(str(repo), [], [command], revision=first)

    assert result["passed"] is True
    assert result["head"] == first


def test_architect_failure_blocks_program(program_env, monkeypatch):
    def boom(repo):
        raise architect.ArchitectError("no envelope")

    monkeypatch.setattr(runner, "run_architect", boom)
    with pytest.raises(runner.ProgramBlockedError, match="Architect failed"):
        runner.start_program("Arch", "reqs", program_env["workspace"])


def test_phase_mission_stays_short_for_command_line(program_env):
    spec = _spec()
    spec["phases"][0]["goal"] = "x" * 20000
    program = {"name": "Big", "spec": spec}
    mission = runner.build_phase_mission(program, 0, "y" * 20000)
    assert len(mission) < 4000


# --- acceptance gate / failure classification (Phase 4) --------------------


def test_case_i_build_and_tests_pass_but_required_criterion_missing_blocks(
    program_env, monkeypatch
):
    """CASE I: even if verify_commands pass, a missing required feature must
    not be reported as completion -- Program stops instead of declaring
    success on build/test alone."""
    spec = _spec(
        phases=1,
        acceptance_criteria=[
            {
                "id": "AC-1",
                "description": "A file the implementation never creates.",
                "verification_type": "file_exists",
                "required": True,
                "path": "never-created.txt",
            }
        ],
    )
    monkeypatch.setattr(runner, "run_architect", lambda repo: spec)

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Missing", "reqs", program_env["workspace"])

    blocked = state.load_program(info.value.program_id)
    assert blocked["status"] == "BLOCKED"
    assert blocked["final"]["last_failure_class"] == fc.REQUIREMENT_UNSATISFIED
    assert "AC-1" in blocked["blocked_reason"]
    # every fix-mission attempt after the first mentions the real gap
    assert any(
        "required acceptance criteria are not met" in m
        for m in program_env["go"].missions
    )


def test_case_i_required_criterion_satisfied_completes(program_env, monkeypatch):
    """CASE A/I positive: once the required criterion's real evidence is
    satisfied, the Program completes."""
    spec = _spec(
        phases=1,
        acceptance_criteria=[
            {
                "id": "AC-1",
                "description": "Requirements captured.",
                "verification_type": "file_exists",
                "required": True,
                "path": "REQUIREMENTS.md",
            }
        ],
    )
    monkeypatch.setattr(runner, "run_architect", lambda repo: spec)

    program = runner.start_program("Present", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    assert program["final"]["acceptance"][0]["status"] == acceptance.SATISFIED


def test_case_h_repeated_verification_failure_reports_bounded_blocker(program_env):
    """CASE H: a persistent deterministic verification failure stops after
    the bounded attempt budget, with the criterion/evidence/attempt count
    in the blocker."""
    program_env["verify"].outcomes = [False] * runner.MAX_ATTEMPTS_PER_STAGE

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Bounded", "reqs", program_env["workspace"])

    blocked = state.load_program(info.value.program_id)
    assert blocked["phases"][0]["attempts"] == runner.MAX_ATTEMPTS_PER_STAGE
    assert blocked["phases"][0]["last_failure_class"] == fc.VERIFICATION_FAILED
    assert "AssertionError" in blocked["blocked_reason"]


def test_case_g_policy_boundary_stops_without_spending_attempt_budget(program_env):
    """A POLICY_BLOCKED-classified run failure (a Worker exceeding its
    declared authority) must stop the stage immediately -- no coding retry
    can fix a policy boundary, and existing systems already decided it."""
    program_env["go"].outcomes = ["READ_ONLY_WORKER_MODIFIED_REPOSITORY"]

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Policy", "reqs", program_env["workspace"])

    blocked = state.load_program(info.value.program_id)
    assert blocked["phases"][0]["attempts"] == 1
    assert blocked["phases"][0]["last_failure_class"] == fc.POLICY_BLOCKED
    assert "POLICY_BLOCKED" in blocked["blocked_reason"]


def test_route_unavailable_fails_fast_without_spending_coding_budget(program_env):
    """CASE D: a transient/route failure must not turn into a corrective
    coding "fix" mission -- the retry is a fresh attempt of the same
    implement mission."""
    program_env["go"].outcomes = ["ESCALATION_UNAVAILABLE"]

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Route unavailable", "reqs", program_env["workspace"])

    blocked = state.load_program(info.value.program_id)
    assert blocked["phases"][0]["attempts"] == 1
    assert blocked["phases"][0]["last_failure_class"] == fc.ROUTE_UNAVAILABLE
    assert len(program_env["go"].missions) == 1


def test_transient_provider_still_retries_same_mission_not_a_fix(program_env):
    program_env["go"].outcomes = [
        ("WORKER_EXECUTION_FAILED", {"worker": {"reason": "TRANSIENT_PROVIDER_ERROR"}})
    ]

    program = runner.start_program("Transient", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    missions = program_env["go"].missions
    assert "verification failed" not in missions[1]
    assert "Phase 1 of 2" in missions[1]


def test_case_k_corrected_revision_is_reverified(program_env, monkeypatch):
    """CASE K: once a fix mission lands, acceptance criteria are evaluated
    again against the NEW revision, not assumed satisfied from before."""
    calls = []
    real_evaluate = acceptance.evaluate_criteria

    def spying_evaluate(repo, revision, criteria, timeout):
        calls.append(revision)
        return real_evaluate(repo, revision, criteria, timeout)

    monkeypatch.setattr(runner.acceptance, "evaluate_criteria", spying_evaluate)
    program_env["verify"].outcomes = [False, True]

    program = runner.start_program("Reverify", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    # the failed verification never reaches acceptance evaluation; only the
    # eventual passing revision does, and it is evaluated fresh each time.
    assert len(calls) >= 1


def test_capability_insufficient_directs_manager_to_stronger_tier(program_env):
    """Section 4/Case B: a CAPABILITY_FAILURE-classified Reviewer verdict
    must not just repeat a generic implement mission -- Program nudges the
    Manager's own route_class/model selection (never hardcoding a model,
    never building a second escalation engine) so the next fresh plan is
    not the same incapable route blindly repeated."""
    program_env["go"].outcomes = [
        (
            "REVIEW_BLOCKED",
            {
                "review": {
                    "verdict": "BLOCKED",
                    "failure_class": "CAPABILITY_FAILURE",
                    "summary": "cc/claude-sonnet-5-low could not resolve the migration.",
                }
            },
        )
    ]

    program = runner.start_program("Capability", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    missions = program_env["go"].missions
    assert "CAPABILITY_INSUFFICIENT" in missions[1]
    assert "select a stronger, more capable route_class/model tier" in missions[1]
    assert "cc/claude-sonnet-5-low could not resolve" in missions[1]


def test_capability_insufficient_excludes_model_structurally_not_only_prompt(program_env):
    """Section (hardening): the natural-language hint must not be the only
    mechanism -- Program must also pass the exact insufficient model to
    run_go's structured exclude_models, which the real routing layer
    enforces via the existing blocked_models eligibility gate."""
    program_env["go"].outcomes = [
        (
            "REVIEW_BLOCKED",
            {
                "review": {"verdict": "BLOCKED", "failure_class": "CAPABILITY_FAILURE", "summary": "too weak"},
                "worker_route": {"route_class": "complex-coding", "model": "cc/claude-sonnet-5-low"},
            },
        )
    ]

    program = runner.start_program("Excl", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    calls = program_env["go"].exclude_models_calls
    assert calls[0] == frozenset()  # first attempt: nothing excluded yet
    assert calls[1] == frozenset({"cc/claude-sonnet-5-low"})
    assert program["phases"][0]["excluded_models"] == ["cc/claude-sonnet-5-low"]


def test_excluded_models_accumulate_across_distinct_insufficient_routes(program_env):
    program_env["go"].outcomes = [
        (
            "REVIEW_BLOCKED",
            {
                "review": {"verdict": "BLOCKED", "failure_class": "CAPABILITY_FAILURE", "summary": "a"},
                "worker_route": {"route_class": "complex-coding", "model": "model-A"},
            },
        ),
        (
            "REVIEW_BLOCKED",
            {
                "review": {"verdict": "BLOCKED", "failure_class": "CAPABILITY_FAILURE", "summary": "b"},
                "worker_route": {"route_class": "complex-coding", "model": "model-B"},
            },
        ),
    ]

    program = runner.start_program("Excl2", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    assert program["phases"][0]["excluded_models"] == ["model-A", "model-B"]
    assert program_env["go"].exclude_models_calls[2] == frozenset({"model-A", "model-B"})


def test_tool_failure_does_not_trigger_capability_directive_or_fix_mission(program_env):
    """Case C: TOOL_FAILURE must not look like a capability gap or a code
    defect -- no escalation directive, and the retry is a fresh implement
    mission, never a "fix the code" mission."""
    program_env["go"].outcomes = [
        (
            "REVIEW_BLOCKED",
            {"review": {"verdict": "BLOCKED", "failure_class": "TOOL_FAILURE", "summary": "pytest crashed."}},
        )
    ]

    program = runner.start_program("ToolFail", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    missions = program_env["go"].missions
    assert "select a stronger, more capable route_class" not in missions[1]
    assert "verification failed" not in missions[1]
    assert program["phases"][0]["last_failure_class"] is None  # cleared on success


def test_final_blocker_preserves_capability_insufficient_not_unknown(program_env):
    """Case D: after bounded attempts, the final blocker reports the real
    last-known failure class, never UNKNOWN_FAILURE, for a known review
    failure."""
    program_env["go"].outcomes = [
        (
            "REVIEW_BLOCKED",
            {"review": {"verdict": "BLOCKED", "failure_class": "CAPABILITY_FAILURE", "summary": "still too weak."}},
        )
    ] * runner.MAX_ATTEMPTS_PER_STAGE

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.start_program("Bounded2", "reqs", program_env["workspace"])

    blocked = state.load_program(info.value.program_id)
    assert blocked["phases"][0]["last_failure_class"] == fc.CAPABILITY_INSUFFICIENT
    assert blocked["phases"][0]["attempts"] == runner.MAX_ATTEMPTS_PER_STAGE
    assert "CAPABILITY_INSUFFICIENT" in blocked["blocked_reason"]
    assert "still too weak" in blocked["blocked_reason"]


def test_requirement_failure_corrective_context_includes_real_reason(program_env):
    """Case A/E: the NEXT corrective attempt must see the concrete reviewer
    reason, not only the bare REVIEW_BLOCKED code."""
    program_env["go"].outcomes = [
        (
            "REVIEW_BLOCKED",
            {
                "review": {
                    "verdict": "BLOCKED",
                    "failure_class": "REQUIREMENT_FAILURE",
                    "summary": "The /inventory endpoint was never implemented.",
                }
            },
        )
    ]

    program = runner.start_program("ReqFail", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    missions = program_env["go"].missions
    assert "REQUIREMENT_UNSATISFIED" in missions[1]
    assert "REQUIREMENT_FAILURE" in missions[1]
    assert "/inventory endpoint was never implemented" in missions[1]
    assert missions[1] != "REVIEW_BLOCKED (worker_ordinal=1)"


def test_bounded_retries_unchanged_across_failure_classes(program_env):
    """Case F: the correction budget itself is untouched by classification."""
    program_env["go"].outcomes = [
        ("REVIEW_BLOCKED", {"review": {"verdict": "BLOCKED", "failure_class": "CAPABILITY_FAILURE", "summary": "x"}}),
        ("REVIEW_BLOCKED", {"review": {"verdict": "BLOCKED", "failure_class": "TOOL_FAILURE", "summary": "y"}}),
        ("REVIEW_BLOCKED", {"review": {"verdict": "BLOCKED", "failure_class": "REQUIREMENT_FAILURE", "summary": "z"}}),
    ]

    program = runner.start_program("Mixed", "reqs", program_env["workspace"])

    assert program["status"] == "COMPLETED"
    assert program["phases"][0]["attempts"] == 4  # 3 failures + 1 success, still bounded


def test_final_acceptance_result_persists_for_resume(program_env):
    """CASE J: acceptance evidence for a DONE stage survives a resume
    without being needlessly recomputed."""
    program = runner.start_program("Persisted", "reqs", program_env["workspace"])
    assert program["status"] == "COMPLETED"

    resumed = runner.resume_program(program["program_id"])
    assert resumed["status"] == "COMPLETED"
    assert resumed["final"]["acceptance"][0]["id"] == "AC-1"
