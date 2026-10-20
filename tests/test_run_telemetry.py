from nexus.telemetry import get_run_summary, record_call, start_run


def test_run_telemetry_aggregates_roles_tokens_and_retries():
    start_run("RUN-T", "DIRECT")
    record_call("RUN-T", role="WORKER", model="free/model", provider="openrouter", usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})
    record_call("RUN-T", role="WORKER", model="free/model", provider="openrouter", usage={"input_tokens": 1, "output_tokens": 1}, reason="RETRY")
    summary = get_run_summary("RUN-T")
    assert summary["execution_strategy"] == "DIRECT"
    assert summary["llm_calls"] == 2
    assert summary["total_tokens"] == 7
    assert summary["retry_count"] == 1
    assert summary["providers_used"] == ["openrouter"]


def test_unknown_usage_is_safe_and_free_cost_can_be_zero():
    start_run("RUN-U", "DELEGATED")
    record_call("RUN-U", role="MANAGER", model="free/model", provider="openrouter", estimated_cost=0)
    summary = get_run_summary("RUN-U")
    assert summary["input_tokens"] is None
    assert summary["output_tokens"] is None
    assert summary["estimated_cost"] == 0


def test_parent_task_metadata_is_preserved():
    start_run(
        "RUN-CONTEXT",
        "DIRECT",
        task_type="CODE_CHANGE",
        execution_mode="TASK",
    )
    summary = get_run_summary("RUN-CONTEXT")
    assert summary["task_type"] == "CODE_CHANGE"
    assert summary["execution_mode"] == "TASK"


def test_capability_and_authority_metadata_is_preserved():
    start_run(
        "RUN-PREFLIGHT",
        "DIRECT",
        task_type="CODE_CHANGE",
        execution_mode="TASK",
        required_capabilities=("FILE_READ", "FILE_WRITE"),
        unsupported_capabilities=(),
        authority_result="AUTHORIZED",
        autonomy_profile="LOCAL_AUTONOMOUS",
    )
    summary = get_run_summary("RUN-PREFLIGHT")
    assert summary["required_capabilities"] == ["FILE_READ", "FILE_WRITE"]
    assert summary["unsupported_capabilities"] == []
    assert summary["authority_result"] == "AUTHORIZED"
    assert summary["autonomy_profile"] == "LOCAL_AUTONOMOUS"
