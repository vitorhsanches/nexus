"""Program runner: requirements prompt -> finished, verified project.

    bootstrap  -> new git repo + REQUIREMENTS.md, registered as a project
    architect  -> docs/NEXUS_SPEC.md (stack, architecture, phases, commands)
    phases     -> for each phase:
                    run_go(phase mission)            # Manager + Workers + Review
                    run_verification(phase commands) # real build/tests
                    on failure: run_go(fix mission) and verify again
    final      -> final verification (+ fix loop) and FINAL_REPORT.md

Fully autonomous: no step waits for a human. A phase that exhausts its
attempt budget stops the Program as BLOCKED; ``resume_program`` continues
from the first unfinished phase with a fresh budget.
"""

from __future__ import annotations

from pathlib import Path

from nexus import failure_classification as fc
from nexus.orchestration.go import GoError, run_go
from nexus.process_timeout import timeout_from_env
from nexus.program import acceptance
from nexus.program.architect import (
    ArchitectError,
    render_spec_markdown,
    run_architect,
)
from nexus.program.bootstrap import REQUIREMENTS_FILE, bootstrap_project, commit_all, git
from nexus.program.state import (
    acquire_ownership,
    append_event,
    load_program,
    new_program_id,
    now_iso,
    release_ownership,
    save_program,
)
from nexus.program.verify import (
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    failure_excerpt,
    run_verification,
)


SPEC_FILE = "docs/NEXUS_SPEC.md"
REPORT_FILE = "FINAL_REPORT.md"
MAX_ATTEMPTS_PER_STAGE = 4
MAX_ARCHITECT_ATTEMPTS = 2
FINAL_STAGE_TITLE = "Final verification"


class ProgramBlockedError(RuntimeError):
    def __init__(self, program_id: str, message: str):
        super().__init__(message)
        self.program_id = program_id


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _commands(commands: list[str]) -> str:
    return "; ".join(f"`{c}`" for c in commands) or "none"


def build_phase_mission(
    program: dict,
    index: int,
    previous_failure: str | None,
    failure_class: str | None = None,
) -> str:
    spec = program["spec"]
    phase = spec["phases"][index]
    total = len(spec["phases"])
    lines = [
        f"Program {program['name']}: implement Phase {index + 1} of {total} - "
        f"{phase['title']}.",
        f"Before planning, read {REQUIREMENTS_FILE} (product requirements) and "
        f"{SPEC_FILE} (approved stack, architecture and phase list) in the "
        "repository.",
    ]
    if index:
        lines.append(
            f"Phases 1-{index} are already implemented and committed; build on "
            "the existing code instead of recreating it."
        )
    lines += [
        f"Phase goal: {_clip(phase['goal'], 1500)}",
        f"Acceptance criteria: {_clip(phase['acceptance_criteria'], 800)}",
        f"Verification (from the repository root): setup {_commands(spec['setup_commands'])}; "
        f"then {_commands(phase['verify_commands'])}. All must pass.",
        "Implement only this phase. Workers that write code also add or update "
        "the tests covering it.",
    ]
    if previous_failure:
        lines.append(
            f"A previous attempt at this phase failed: {_clip(previous_failure, 600)}"
        )
    if failure_class == fc.CAPABILITY_INSUFFICIENT:
        # The Reviewer's own verdict was BLOCKED (not ESCALATE), so
        # progressive.py's existing model-escalation ladder never ran for
        # that attempt -- this is a fresh run_go call with its own new
        # Manager plan, so nudge the Manager's own route_class/model
        # selection instead of Program hardcoding a model or building a
        # second escalation engine.
        lines.append(
            "The Reviewer judged the capability tier used previously "
            "insufficient for this phase. When planning this Worker, select "
            "a stronger, more capable route_class/model tier than the one "
            "that already proved insufficient, per your own routing policy."
        )
    return "\n".join(lines)


def build_fix_mission(
    program: dict,
    stage_title: str,
    commands: list[str],
    excerpt: str,
    failure_class: str | None = None,
) -> str:
    spec = program["spec"]
    if failure_class == fc.REQUIREMENT_UNSATISFIED:
        headline = (
            f"Program {program['name']}, {stage_title}: build and tests pass, but "
            "required acceptance criteria are not met -- a requested feature is "
            "missing or incomplete."
        )
    else:
        headline = f"Program {program['name']}, {stage_title}: verification failed."
    return "\n".join(
        [
            headline,
            f"Context: {REQUIREMENTS_FILE} and {SPEC_FILE} in the repository.",
            f"Verification: setup {_commands(spec['setup_commands'])}; then "
            f"{_commands(commands)}.",
            _clip(excerpt, 2600),
            "Fix the code so verification and every listed acceptance criterion "
            "pass. Do not weaken, skip or remove tests or checks to make them "
            "pass.",
        ]
    )


def _run_stage(
    program: dict,
    stage: dict,
    implement_mission,
    verify_commands: list[str],
    criteria: list[dict] = (),
    expected_revision: str | None = None,
) -> bool:
    """Implement/verify/accept/fix loop for one phase (or the final stage).

    ``implement_mission`` is ``None`` for the final stage, which has nothing
    to implement and starts by verifying. A stage only returns True once
    verification passes AND every required acceptance ``criteria`` entry is
    SATISFIED against that exact revision -- passing build/test alone is
    not enough (a requested feature can be missing while tests still pass).

    A failure classified as a policy/authority/capability boundary
    (``fc.blocks_coding_retry``), or a deterministic Worker runtime/
    infrastructure failure (Phase 4.6, ``fc.is_deterministic_worker_
    infrastructure_failure``), stops the stage immediately instead of
    spending its attempt budget: no amount of retrying changes a
    deterministic block that existing systems already decided, or that no
    repository content could ever fix.

    ``expected_revision`` (Phase 4.5, Part 3), when given, is the primary
    repo revision Nexus already integrated for this Program -- either this
    exact stage's own prior revision (a crashed attempt already landed
    real code for it, Part 4) or the most recent completed predecessor
    phase's revision. Checked once, before any new implementation work:
    a mismatch means the repository changed outside Nexus (checkout,
    reset, rebase, amend, another tool) while it was stopped, and the
    stage fails closed instead of silently building on unexpected code.
    ``None`` means nothing has been integrated yet (a fresh Program's
    first phase) -- never a false blocker.
    """
    repo = program["path"]
    spec = program["spec"]
    verify_timeout = timeout_from_env(
        "NEXUS_VERIFY_TIMEOUT_SECONDS", DEFAULT_VERIFY_TIMEOUT_SECONDS
    )

    if expected_revision:
        actual_revision = git(["rev-parse", "HEAD"], Path(repo)).stdout.strip()
        if actual_revision != expected_revision:
            stage["last_failure_class"] = fc.ENVIRONMENT_FAILURE
            stage["last_error"] = (
                "Primary repository HEAD "
                f"({actual_revision}) does not match the revision Nexus "
                f"last integrated for this Program ({expected_revision}). "
                "The repository may have changed outside Nexus (checkout, "
                "reset, rebase, amend, or another tool) while it was "
                "stopped. Not continuing automatically."
            )
            append_event(
                program,
                "REVISION_MISMATCH",
                stage=stage["title"],
                expected=expected_revision,
                actual=actual_revision,
            )
            save_program(program)
            return False

    previous_failure: str | None = stage.get("last_error")
    needs_fix: str | None = None
    verify_first = implement_mission is None
    target_revision: str | None = stage.get("target_revision")

    while stage["attempts"] < MAX_ATTEMPTS_PER_STAGE:
        if not verify_first:
            stage["attempts"] += 1
            if needs_fix:
                mission = build_fix_mission(
                    program,
                    stage["title"],
                    verify_commands,
                    needs_fix,
                    failure_class=stage.get("last_failure_class"),
                )
                kind = "fix"
            else:
                mission = implement_mission(previous_failure)
                kind = "implement"

            append_event(program, "RUN_STARTED", stage=stage["title"], mission_kind=kind)
            save_program(program)
            try:
                result = run_go(
                    mission,
                    project_query=program["project_id"],
                    planning_required=True,
                    exclude_models=frozenset(stage.get("excluded_models") or ()),
                )
                stage["run_ids"].append(result["run_id"])
                target_revision = git(
                    ["rev-parse", "HEAD"], Path(repo)
                ).stdout.strip()
                stage["target_revision"] = target_revision
                append_event(
                    program,
                    "RUN_COMPLETED",
                    stage=stage["title"],
                    run_id=result["run_id"],
                    revision=target_revision,
                )
                # Phase 4.5, Part 2: run_go() already merged real code into
                # the primary repo by this point -- persist that fact
                # (run_id + exact revision) BEFORE the potentially
                # many-minutes-long run_verification() below. A crash
                # during verification must never lose track of an
                # already-integrated revision.
                save_program(program)
            except GoError as error:
                if error.run_id:
                    stage["run_ids"].append(error.run_id)
                failure_class = fc.classify_go_error(error)
                stage["last_failure_class"] = failure_class
                if failure_class == fc.CAPABILITY_INSUFFICIENT:
                    # Structured hardening: exclude the exact model the
                    # Reviewer already judged insufficient from the SAME
                    # capability's next route selection (existing
                    # blocked_models routing gate), not only a
                    # natural-language hint the Manager may or may not
                    # follow. Accumulated across attempts so a second,
                    # different insufficient model is excluded too.
                    worker_route = (error.detail or {}).get("worker_route") or {}
                    failed_model = worker_route.get("model")
                    if failed_model:
                        excluded = set(stage.get("excluded_models") or ())
                        excluded.add(failed_model)
                        stage["excluded_models"] = sorted(excluded)
                # Phase 4.1: read the real Reviewer/Worker evidence from
                # error.detail (fc.describe_go_error) instead of the bare
                # "Run failed with REVIEW_BLOCKED: REVIEW_BLOCKED
                # (worker_ordinal=1)" text -- the next corrective mission
                # needs to know WHAT was missed, not just that a Run failed.
                previous_failure = (
                    f"[{failure_class}] {fc.describe_go_error(error)}"
                )
                stage["last_error"] = previous_failure
                append_event(
                    program,
                    "RUN_FAILED",
                    stage=stage["title"],
                    run_id=error.run_id,
                    code=error.code,
                    failure_class=failure_class,
                )
                save_program(program)
                if fc.blocks_coding_retry(failure_class) or fc.is_deterministic_worker_infrastructure_failure(error):
                    # Deterministic policy/authority/capability boundary,
                    # or (Phase 4.6) a deterministic Worker runtime/tooling
                    # failure -- existing systems already decided this, or
                    # no repository content could ever fix it; do not
                    # spend another coding attempt on it.
                    return False
                # A failed run changes nothing in the primary repo; retry
                # the same kind of mission with the failure as context.
                continue
        verify_first = False

        if target_revision is None:
            target_revision = git(
                ["rev-parse", "HEAD"], Path(repo)
            ).stdout.strip()
            stage["target_revision"] = target_revision
        verification = run_verification(
            repo,
            spec["setup_commands"],
            verify_commands,
            revision=target_revision,
        )
        stage["last_verification"] = verification

        if not verification["passed"]:
            stage["acceptance"] = acceptance.unverified(criteria)
            stage["acceptance_summary"] = acceptance.summarize(stage["acceptance"])
            needs_fix = failure_excerpt(verification)
            stage["last_error"] = _clip(needs_fix, 1200)
            stage["last_failure_class"] = fc.classify_verification_failure()
            append_event(
                program,
                "VERIFY_FAILED",
                stage=stage["title"],
                head=verification["head"],
                failure_class=stage["last_failure_class"],
            )
            save_program(program)
            continue

        acceptance_results = (
            acceptance.evaluate_criteria(repo, verification["head"], criteria, verify_timeout)
            if criteria
            else []
        )
        stage["acceptance"] = acceptance_results
        stage["acceptance_summary"] = acceptance.summarize(acceptance_results)

        if acceptance.gate_passed(acceptance_results):
            stage["last_error"] = None
            stage["last_failure_class"] = None
            append_event(
                program,
                "VERIFY_PASSED",
                stage=stage["title"],
                head=verification["head"],
                **stage["acceptance_summary"],
            )
            save_program(program)
            return True

        needs_fix = acceptance.render_failure_excerpt(acceptance_results)
        stage["last_error"] = _clip(needs_fix, 1200)
        stage["last_failure_class"] = fc.REQUIREMENT_UNSATISFIED
        append_event(
            program,
            "ACCEPTANCE_FAILED",
            stage=stage["title"],
            head=verification["head"],
            **stage["acceptance_summary"],
        )
        save_program(program)

    return False


def _specify(program: dict) -> None:
    last_error = None
    for _ in range(MAX_ARCHITECT_ATTEMPTS):
        try:
            spec = run_architect(program["path"])
            break
        except ArchitectError as error:
            last_error = str(error)
            append_event(program, "ARCHITECT_FAILED", error=_clip(last_error, 800))
            save_program(program)
    else:
        _block(program, f"Architect failed: {last_error}")

    spec_path = Path(program["path"]) / SPEC_FILE
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(render_spec_markdown(program["name"], spec), encoding="utf-8")
    commit_all(Path(program["path"]), "docs: add Nexus implementation spec")

    program["spec"] = spec
    program["phases"] = [_new_stage(phase["title"]) for phase in spec["phases"]]
    program["final"] = _new_stage(FINAL_STAGE_TITLE)
    program["status"] = "SPECIFIED"
    append_event(program, "SPECIFIED", phases=len(spec["phases"]))
    save_program(program)


def _new_stage(title: str) -> dict:
    return {
        "title": title,
        "status": "PENDING",
        "attempts": 0,
        "run_ids": [],
        "last_error": None,
        "last_failure_class": None,
        "excluded_models": [],
        "target_revision": None,
        "acceptance": [],
        "acceptance_summary": acceptance.summarize([]),
    }


def _last_integrated_revision(phases: list[dict]) -> str | None:
    """Most recent revision Nexus actually integrated among ``phases``, or
    ``None`` if none of them have run yet."""
    for phase in reversed(phases):
        if phase.get("target_revision"):
            return phase["target_revision"]
    return None


def _expected_revision(stage: dict, predecessor_phases: list[dict]) -> str | None:
    """Phase 4.5, Part 3: the primary repo revision Nexus expects before
    starting/continuing ``stage`` -- its own already-integrated revision
    (Part 4: a crashed attempt already landed real code for this exact
    stage) takes priority over the most recent completed predecessor's
    revision. ``None`` for a fresh Program with nothing integrated yet."""
    return stage.get("target_revision") or _last_integrated_revision(predecessor_phases)


def _block(program: dict, message: str):
    program["status"] = "BLOCKED"
    program["blocked_reason"] = message
    append_event(program, "BLOCKED", reason=_clip(message, 800))
    release_ownership(program)  # also saves; a deterministic exit path
    raise ProgramBlockedError(program["program_id"], message)


def _acceptance_lines(results: list[dict]) -> list[str]:
    if not results:
        return ["(no acceptance criteria declared for this scope)"]
    lines = ["| Criterion | Required | Status | Evidence |", "|---|---|---|---|"]
    for result in results:
        evidence = result["evidence"].splitlines()[0][:160].replace("|", "\\|")
        lines.append(
            f"| [{result['id']}] {result['description']} | "
            f"{'yes' if result['required'] else 'no'} | {result['status']} | {evidence} |"
        )
    return lines


def program_acceptance_summary(program: dict) -> dict:
    """Aggregate acceptance counts across every phase and the final stage."""
    totals = acceptance.summarize([])
    for stage in [*program.get("phases", []), program.get("final") or {}]:
        if not stage:
            continue
        for key, value in (stage.get("acceptance_summary") or {}).items():
            totals[key] = totals.get(key, 0) + value
    return totals


def write_report(program: dict) -> str:
    spec = program["spec"]
    lines = [
        f"# {program['name']} — Final Report",
        "",
        f"Built autonomously by Nexus Program `{program['program_id']}` "
        f"({program['created_at']} → {now_iso()}).",
        "",
        f"**Result: {program['status']}**",
        "",
        "## Summary",
        "",
        spec["summary"],
        "",
        "## Stack",
        "",
        spec["stack"],
        "",
        "## How to run",
        "",
        "```",
        *spec["setup_commands"],
        *spec["final_verify_commands"],
        "```",
        "",
        "## Requirements → Acceptance Criteria",
        "",
        f"Original request: see `{REQUIREMENTS_FILE}`. Full design: see "
        "`docs/NEXUS_SPEC.md`.",
        "",
        *_acceptance_lines(program["final"].get("acceptance") or []),
        "",
        "## Phases",
        "",
        "| # | Phase | Status | Attempts | Failure class | Runs |",
        "|---|-------|--------|----------|----------------|------|",
    ]
    for number, phase in enumerate(program["phases"], start=1):
        lines.append(
            f"| {number} | {phase['title']} | {phase['status']} | "
            f"{phase['attempts']} | {phase.get('last_failure_class') or '-'} | "
            f"{', '.join(phase['run_ids']) or '-'} |"
        )
        lines += ["", f"**Phase {number} acceptance:**", "", *_acceptance_lines(phase.get("acceptance") or []), ""]

    final = program["final"]
    verification = final.get("last_verification") or {}
    lines += ["## Final verification", ""]
    for result in verification.get("results", []):
        mark = "PASS" if result["passed"] else "FAIL"
        lines.append(f"- `{result['command']}` — {mark}")

    unresolved = [
        r
        for r in program["final"].get("acceptance") or []
        if r["required"] and r["status"] != acceptance.SATISFIED
    ]
    lines += ["", "## Unresolved criteria", ""]
    if unresolved:
        for r in unresolved:
            lines.append(f"- [{r['id']}] {r['description']} — {r['status']}: {r['evidence'].splitlines()[0]}")
    else:
        lines.append("None.")

    lines += ["", ""]
    report = "\n".join(lines)
    (Path(program["path"]) / REPORT_FILE).write_text(report, encoding="utf-8")
    commit_all(Path(program["path"]), "docs: add Nexus final report")
    return report


def _execute(program: dict) -> dict:
    if program["status"] == "BOOTSTRAPPED":
        _specify(program)
        if program.get("stop_after_spec"):
            release_ownership(program)  # normal pause, not a crash
            return program

    program["status"] = "RUNNING"
    save_program(program)

    spec = program["spec"]
    for index, phase_spec in enumerate(spec["phases"]):
        stage = program["phases"][index]
        if stage["status"] == "DONE":
            continue

        print()
        print(f"NEXUS PROGRAM - Phase {index + 1}/{len(spec['phases'])}: {phase_spec['title']}")
        print("=" * 70)

        stage["status"] = "RUNNING"
        save_program(program)

        ok = _run_stage(
            program,
            stage,
            lambda failure, i=index: build_phase_mission(
                program, i, failure, stage.get("last_failure_class")
            ),
            phase_spec["verify_commands"],
            phase_spec["criteria"],
            expected_revision=_expected_revision(stage, program["phases"][:index]),
        )
        if not ok:
            stage["status"] = "BLOCKED"
            _block(
                program,
                f"Phase {index + 1} ({phase_spec['title']}) did not pass after "
                f"{stage['attempts']} attempts "
                f"[{stage.get('last_failure_class') or 'UNKNOWN'}]: "
                f"{stage.get('last_error')}",
            )
        stage["status"] = "DONE"
        save_program(program)

    final = program["final"]
    if final["status"] != "DONE":
        print()
        print("NEXUS PROGRAM - Final verification")
        print("=" * 70)
        final["status"] = "RUNNING"
        save_program(program)
        if not _run_stage(
            program, final, None, spec["final_verify_commands"], spec["acceptance_criteria"],
            expected_revision=_expected_revision(final, program["phases"]),
        ):
            final["status"] = "BLOCKED"
            _block(
                program,
                f"Final verification failed "
                f"[{final.get('last_failure_class') or 'UNKNOWN'}]: "
                f"{final.get('last_error')}",
            )
        final["status"] = "DONE"

    write_report(program)
    program["status"] = "COMPLETED"
    append_event(program, "COMPLETED")
    release_ownership(program)  # also saves; deterministic normal exit
    return program


def _execute_with_terminal_cleanup(program: dict) -> dict:
    """Release a live owner on a terminal Python exit; hard process death remains reconcilable."""
    try:
        return _execute(program)
    except ProgramBlockedError:
        raise  # _block already released ownership
    except Exception:
        program["status"] = "BLOCKED"
        append_event(program, "TERMINAL_EXCEPTION")
        release_ownership(program)
        raise


def start_program(
    name: str,
    requirements: str,
    workspace_root: Path | None = None,
    stop_after_spec: bool = False,
    task_contract: dict | None = None,
) -> dict:
    project = bootstrap_project(name, requirements, workspace_root)
    program = {
        "program_id": new_program_id(),
        "name": name,
        "project_id": project["project_id"],
        "path": project["path"],
        "status": "BOOTSTRAPPED",
        "created_at": now_iso(),
        "stop_after_spec": stop_after_spec,
        "spec": None,
        "phases": [],
        "final": None,
        "events": [],
        "task_contract": task_contract,
    }
    append_event(program, "BOOTSTRAPPED", path=project["path"])
    save_program(program)
    # Phase 4.5, Part 1: claim execution ownership before any real work
    # begins. A brand-new program_id is never owned yet, so this only
    # ever stamps it -- it exists so a premature concurrent
    # ``--resume <this program_id>`` (however unlikely) is refused rather
    # than racing this invocation.
    program = acquire_ownership(program["program_id"])

    print()
    print("NEXUS PROGRAM")
    print("=" * 70)
    print(f"Program : {program['program_id']}")
    print(f"Project : {project['project_id']}")
    print(f"Repo    : {project['path']}")

    return _execute_with_terminal_cleanup(program)


def resume_program(program_id: str) -> dict:
    program = load_program(program_id)
    if program["status"] == "COMPLETED":
        return program

    # Phase 4.5, Part 1: claim ownership BEFORE any state mutation below.
    # Raises ProgramOwnershipError (PROGRAM_OWNER_ACTIVE / _FOREIGN) and
    # leaves the Program completely untouched when a live process already
    # owns it -- no second Manager/Worker is ever launched concurrently.
    program = acquire_ownership(program_id)

    # A resume is an explicit request to try again: blocked/interrupted
    # stages get a fresh attempt budget.
    for stage in [*program.get("phases", []), program.get("final") or {}]:
        if stage and stage.get("status") in ("BLOCKED", "RUNNING"):
            verified = (stage.get("last_verification") or {}).get("passed")
            accepted = acceptance.gate_passed(stage.get("acceptance") or [])
            if verified and accepted:
                stage["status"] = "DONE"
            else:
                stage["status"] = "PENDING"
                stage["attempts"] = 0

    program["stop_after_spec"] = False
    program["blocked_reason"] = None
    if program["status"] == "BLOCKED":
        program["status"] = "SPECIFIED" if program.get("spec") else "BOOTSTRAPPED"
    append_event(program, "RESUMED")
    save_program(program)
    return _execute_with_terminal_cleanup(program)
