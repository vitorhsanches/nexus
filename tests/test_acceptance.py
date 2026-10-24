import pytest

from nexus.program import acceptance, bootstrap


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    bootstrap.git(["init", "-b", "main"], repo)
    (repo / "present.txt").write_text("x", encoding="utf-8")
    bootstrap.commit_all(repo, "init")
    return repo


def _criterion(**overrides):
    base = {
        "id": "AC-1",
        "description": "desc",
        "verification_type": "file_exists",
        "required": True,
        "path": "present.txt",
    }
    base.update(overrides)
    return base


# --- validate_criteria -------------------------------------------------------


def test_validate_criteria_accepts_command_and_file_exists():
    seen = set()
    result = acceptance.validate_criteria(
        [
            _criterion(id="AC-1"),
            {
                "id": "AC-2",
                "description": "runs",
                "verification_type": "command",
                "required": False,
                "command": "npm test",
            },
        ],
        field="acceptance_criteria",
        seen_ids=seen,
        allow_empty=False,
    )
    assert {c["id"] for c in result} == {"AC-1", "AC-2"}
    assert seen == {"AC-1", "AC-2"}


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"description": "x", "verification_type": "file_exists", "path": "a"}],
        [_criterion(description="")],
        [_criterion(verification_type="bogus")],
        [_criterion(required="yes")],
        [{"id": "AC-1", "description": "x", "verification_type": "command"}],
        [_criterion(path="/etc/passwd")],
        [_criterion(path="../escape")],
        "not-a-list",
    ],
)
def test_validate_criteria_fails_closed(value):
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.validate_criteria(
            value, field="f", seen_ids=set(), allow_empty=False
        )


def test_validate_criteria_rejects_duplicate_ids_across_calls():
    seen = set()
    acceptance.validate_criteria([_criterion(id="AC-1")], field="a", seen_ids=seen, allow_empty=False)
    with pytest.raises(acceptance.AcceptanceError, match="not unique"):
        acceptance.validate_criteria(
            [_criterion(id="AC-1")], field="b", seen_ids=seen, allow_empty=False
        )


def test_validate_criteria_allows_empty_when_permitted():
    assert acceptance.validate_criteria([], field="f", seen_ids=set(), allow_empty=True) == []


def test_validate_criteria_enforces_max_count():
    seen = set()
    too_many = [_criterion(id=f"AC-{i}") for i in range(acceptance.MAX_CRITERIA + 1)]
    with pytest.raises(acceptance.AcceptanceError, match="more than"):
        acceptance.validate_criteria(too_many, field="f", seen_ids=seen, allow_empty=False)


# --- evaluate_criteria -------------------------------------------------------


def test_evaluate_file_exists_true_and_false(tmp_path):
    repo = _repo(tmp_path)
    head = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()

    results = acceptance.evaluate_criteria(
        str(repo),
        head,
        [
            _criterion(id="AC-1", path="present.txt"),
            _criterion(id="AC-2", path="missing.txt"),
        ],
        30,
    )

    by_id = {r["id"]: r for r in results}
    assert by_id["AC-1"]["status"] == acceptance.SATISFIED
    assert by_id["AC-2"]["status"] == acceptance.UNSATISFIED
    assert "present.txt" in by_id["AC-1"]["evidence"]


def test_evaluate_command_true_and_false(tmp_path, monkeypatch):
    import sys

    repo = _repo(tmp_path)
    head = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()
    ok = f'"{sys.executable}" -c "print(1)"'
    fail = f'"{sys.executable}" -c "import sys; sys.exit(1)"'

    results = acceptance.evaluate_criteria(
        str(repo),
        head,
        [
            {"id": "AC-1", "description": "ok", "verification_type": "command", "required": True, "command": ok},
            {"id": "AC-2", "description": "bad", "verification_type": "command", "required": True, "command": fail},
        ],
        30,
    )

    by_id = {r["id"]: r for r in results}
    assert by_id["AC-1"]["status"] == acceptance.SATISFIED
    assert by_id["AC-2"]["status"] == acceptance.UNSATISFIED


def test_evaluate_criteria_never_drops_a_criterion(tmp_path):
    repo = _repo(tmp_path)
    head = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()
    criteria = [_criterion(id=f"AC-{i}", path=f"missing-{i}.txt") for i in range(5)]

    results = acceptance.evaluate_criteria(str(repo), head, criteria, 30)

    assert len(results) == 5
    assert {r["id"] for r in results} == {c["id"] for c in criteria}


def test_evaluate_criteria_empty_list_short_circuits_without_git(tmp_path):
    # repo need not even exist for an empty criteria list.
    assert acceptance.evaluate_criteria(str(tmp_path / "nope"), "HEAD", [], 30) == []


# --- gate / summarize / unverified / rendering ------------------------------


def test_gate_passed_ignores_optional_criteria():
    results = [
        {**_criterion(id="AC-1"), "status": acceptance.SATISFIED, "evidence": ""},
        {**_criterion(id="AC-2", required=False), "status": acceptance.UNSATISFIED, "evidence": ""},
    ]
    assert acceptance.gate_passed(results) is True
    assert acceptance.failed_required(results) == []


def test_gate_failed_when_required_unverified_or_unsatisfied():
    for status in (acceptance.UNVERIFIED, acceptance.UNSATISFIED):
        results = [{**_criterion(id="AC-1"), "status": status, "evidence": ""}]
        assert acceptance.gate_passed(results) is False
        assert acceptance.failed_required(results)[0]["id"] == "AC-1"


def test_unverified_marks_every_criterion_without_fabricating_evidence():
    criteria = [_criterion(id="AC-1"), _criterion(id="AC-2", required=False)]
    results = acceptance.unverified(criteria)
    assert all(r["status"] == acceptance.UNVERIFIED for r in results)
    assert all("earlier verification step failed" in r["evidence"] for r in results)


def test_summarize_counts_each_status():
    results = [
        {"status": acceptance.SATISFIED},
        {"status": acceptance.SATISFIED},
        {"status": acceptance.UNSATISFIED},
        {"status": acceptance.UNVERIFIED},
    ]
    counts = acceptance.summarize(results)
    assert counts == {
        "acceptance_total": 4,
        "acceptance_satisfied": 2,
        "acceptance_unsatisfied": 1,
        "acceptance_unverified": 1,
    }


def test_render_failure_excerpt_empty_when_gate_passes():
    results = [{**_criterion(id="AC-1"), "status": acceptance.SATISFIED, "evidence": "ok"}]
    assert acceptance.render_failure_excerpt(results) == ""


def test_render_failure_excerpt_includes_id_and_evidence():
    results = [
        {**_criterion(id="AC-1"), "status": acceptance.UNSATISFIED, "evidence": "path missing"}
    ]
    excerpt = acceptance.render_failure_excerpt(results)
    assert "AC-1" in excerpt and "path missing" in excerpt
