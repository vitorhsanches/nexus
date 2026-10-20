import pytest

from nexus import failure_classification as fc
from nexus.orchestration.go import GoError


def _error(code, detail=None):
    return GoError(code, "failed", run_id="RUN-1", detail=detail)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("INITIAL_ROUTING_UNAVAILABLE", fc.ROUTE_UNAVAILABLE),
        ("ESCALATION_UNAVAILABLE", fc.ROUTE_UNAVAILABLE),
        ("CHECKPOINT_PERSISTENCE_FAILED", fc.ENVIRONMENT_FAILURE),
        ("GIT_TOOL_FAILURE", fc.ENVIRONMENT_FAILURE),
        ("PRIMARY_REPO_CHANGED_UNEXPECTEDLY", fc.ENVIRONMENT_FAILURE),
        ("INTEGRATION_CONFLICT", fc.ENVIRONMENT_FAILURE),
        ("INTEGRATION_DIRTY_OVERLAP", fc.ENVIRONMENT_FAILURE),
        ("READ_ONLY_WORKER_MODIFIED_REPOSITORY", fc.POLICY_BLOCKED),
        ("SCOUT_STAGE_FAILED", fc.ENVIRONMENT_FAILURE),
        ("PLAN_INVALID", fc.UNKNOWN_FAILURE),
        ("SOMETHING_NEW_AND_UNMAPPED", fc.UNKNOWN_FAILURE),
    ],
)
def test_classify_go_error_by_code(code, expected):
    assert fc.classify_go_error(_error(code)) == expected


def test_classify_manager_blocked_distinguishes_launch_from_planning_failure():
    launch = _error("MANAGER_BLOCKED")
    launch.args = ("Manager launch failed: no codex",)
    assert fc.classify_go_error(launch) == fc.ENVIRONMENT_FAILURE

    planning = _error("MANAGER_BLOCKED")
    planning.args = ("Manager planning failed with exit_code=1.",)
    assert fc.classify_go_error(planning) == fc.UNKNOWN_FAILURE


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("TRANSIENT_PROVIDER_ERROR", fc.TRANSIENT_PROVIDER),
        ("WORKER_TIMEOUT", fc.TRANSIENT_PROVIDER),
        ("WORKER_RESULT_MISSING", fc.ENVIRONMENT_FAILURE),
        ("WORKER_RESULT_EMPTY", fc.ENVIRONMENT_FAILURE),
        (None, fc.ENVIRONMENT_FAILURE),
    ],
)
def test_classify_worker_execution_failed_uses_worker_reason(reason, expected):
    error = _error("WORKER_EXECUTION_FAILED", detail={"worker": {"reason": reason}})
    assert fc.classify_go_error(error) == expected


def test_classify_worker_execution_failed_without_detail_is_environment_failure():
    assert fc.classify_go_error(_error("WORKER_EXECUTION_FAILED")) == fc.ENVIRONMENT_FAILURE


@pytest.mark.parametrize(
    "review_failure_class,expected",
    [
        ("TRANSIENT", fc.TRANSIENT_PROVIDER),
        ("PROVIDER_FAILURE", fc.TRANSIENT_PROVIDER),
        ("TOOL_FAILURE", fc.ENVIRONMENT_FAILURE),
        ("CAPABILITY_FAILURE", fc.CAPABILITY_INSUFFICIENT),
        ("SCOPE_VIOLATION", fc.POLICY_BLOCKED),
        ("VALIDATION_FAILURE", fc.VERIFICATION_FAILED),
        ("REQUIREMENT_FAILURE", fc.REQUIREMENT_UNSATISFIED),
        ("UNKNOWN", fc.UNKNOWN_FAILURE),
        (None, fc.UNKNOWN_FAILURE),
    ],
)
def test_classify_review_blocked_uses_reviewer_failure_class(review_failure_class, expected):
    error = _error("REVIEW_BLOCKED", detail={"review": {"failure_class": review_failure_class}})
    assert fc.classify_go_error(error) == expected


def test_classify_review_blocked_without_detail_is_unknown():
    assert fc.classify_go_error(_error("REVIEW_BLOCKED")) == fc.UNKNOWN_FAILURE


def test_classify_verification_failure_is_constant():
    assert fc.classify_verification_failure() == fc.VERIFICATION_FAILED


@pytest.mark.parametrize(
    "authority_result,expected",
    [
        ("AUTHORIZED", None),
        ("NEEDS_APPROVAL", fc.AUTHORITY_REQUIRED),
        ("BLOCKED", fc.POLICY_BLOCKED),
    ],
)
def test_classify_authority(authority_result, expected):
    assert fc.classify_authority(authority_result) == expected


def test_blocks_coding_retry_for_route_policy_authority_and_unsupported():
    for blocked in (
        fc.ROUTE_UNAVAILABLE,
        fc.POLICY_BLOCKED,
        fc.AUTHORITY_REQUIRED,
        fc.UNSUPPORTED_CAPABILITY,
    ):
        assert fc.blocks_coding_retry(blocked) is True
    for allowed in (
        fc.TRANSIENT_PROVIDER,
        fc.CAPABILITY_INSUFFICIENT,
        fc.VERIFICATION_FAILED,
        fc.ENVIRONMENT_FAILURE,
        fc.REQUIREMENT_UNSATISFIED,
        fc.UNKNOWN_FAILURE,
    ):
        assert fc.blocks_coding_retry(allowed) is False


def test_is_transient_like():
    for yes in (fc.TRANSIENT_PROVIDER, fc.CAPABILITY_INSUFFICIENT, fc.ENVIRONMENT_FAILURE):
        assert fc.is_transient_like(yes) is True
    for no in (fc.ROUTE_UNAVAILABLE, fc.VERIFICATION_FAILED, fc.REQUIREMENT_UNSATISFIED, fc.POLICY_BLOCKED, fc.AUTHORITY_REQUIRED):
        assert fc.is_transient_like(no) is False


def test_failure_classes_vocabulary_is_small_and_stable():
    assert fc.FAILURE_CLASSES == {
        "TRANSIENT_PROVIDER",
        "ROUTE_UNAVAILABLE",
        "CAPABILITY_INSUFFICIENT",
        "VERIFICATION_FAILED",
        "ENVIRONMENT_FAILURE",
        "POLICY_BLOCKED",
        "AUTHORITY_REQUIRED",
        "UNSUPPORTED_CAPABILITY",
        "REQUIREMENT_UNSATISFIED",
        "UNKNOWN_FAILURE",
    }
