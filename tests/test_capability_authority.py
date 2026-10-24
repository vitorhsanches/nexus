from unittest.mock import patch

from nexus.authority import (
    AuthorityResult,
    AutonomyProfile,
    resolve_authority,
)
from nexus.capabilities.execution import (
    ExecutionCapability,
    SUPPORTED_EXECUTION_CAPABILITIES,
    resolve_capabilities,
)
from nexus.intake import dispatch_request, normalize_task


def test_capability_registry_matches_real_local_execution_paths():
    assert SUPPORTED_EXECUTION_CAPABILITIES == {
        ExecutionCapability.REPOSITORY_READ,
        ExecutionCapability.FILE_READ,
        ExecutionCapability.FILE_WRITE,
        ExecutionCapability.SHELL_EXECUTION,
        ExecutionCapability.TEST_EXECUTION,
        ExecutionCapability.LOCAL_GIT,
    }
    resolution = resolve_capabilities(
        (
            ExecutionCapability.FILE_READ.value,
            ExecutionCapability.WEB_RESEARCH.value,
        )
    )
    assert resolution.supported == (ExecutionCapability.FILE_READ.value,)
    assert resolution.unsupported == (ExecutionCapability.WEB_RESEARCH.value,)


def test_local_code_change_is_supported_and_authorized():
    contract = normalize_task(
        "Fix config in app.py and run tests.",
        project="FinaSync",
    )
    capability = resolve_capabilities(contract.required_capabilities)
    authority = resolve_authority(contract.goal)
    assert capability.all_supported is True
    assert ExecutionCapability.TEST_EXECUTION.value in capability.required
    assert authority.result is AuthorityResult.AUTHORIZED


def test_natural_language_local_autonomy_preserves_external_restrictions():
    text = (
        "Execute este projeto de forma totalmente autônoma. "
        "Você pode criar arquivos, instalar dependências, executar testes, "
        "corrigir erros e continuar até finalizar. Não faça deploy nem push."
    )
    contract = normalize_task(text)
    capability = resolve_capabilities(contract.required_capabilities)
    authority = resolve_authority(text)
    assert contract.task_type.value == "SOFTWARE_PROJECT"
    assert capability.all_supported is True
    assert authority.autonomy_profile is AutonomyProfile.LOCAL_AUTONOMOUS
    assert authority.result is AuthorityResult.AUTHORIZED
    assert set(authority.prohibited_actions) == {
        "REMOTE_PUSH",
        "EXTERNAL_DEPLOYMENT",
    }


def test_research_is_deterministically_unsupported_and_not_dispatched():
    with patch("nexus.intake.run_go") as run_go, patch(
        "nexus.intake.start_program"
    ) as program:
        result = dispatch_request("Research deterministic scheduling.")
    assert result["result"]["status"] == "UNSUPPORTED_CAPABILITY"
    assert result["result"]["missing"] == ["WEB_RESEARCH"]
    run_go.assert_not_called()
    program.assert_not_called()


def test_external_api_is_unsupported_before_execution():
    with patch("nexus.intake.run_go") as run_go:
        result = dispatch_request(
            "Update app.py by calling an external API.",
            project="FinaSync",
        )
    assert result["result"]["status"] == "UNSUPPORTED_CAPABILITY"
    assert "EXTERNAL_API" in result["result"]["missing"]
    run_go.assert_not_called()


def test_positive_push_is_supported_capability_but_needs_approval():
    with patch("nexus.intake.run_go") as run_go:
        result = dispatch_request(
            "Update package-lock.json, commit and push the changes.",
            project="FinaSync",
            autonomy="local",
        )
    preflight = result["task_contract"]
    assert preflight["capability_resolution"]["unsupported"] == []
    assert preflight["authority_resolution"]["result"] == "NEEDS_APPROVAL"
    assert result["result"]["status"] == "NEEDS_APPROVAL"
    run_go.assert_not_called()


def test_negative_push_restriction_does_not_request_approval():
    with patch(
        "nexus.intake.run_go",
        return_value={"run_id": "RUN-LOCAL", "status": "COMPLETED"},
    ) as run_go:
        result = dispatch_request(
            "Update package-lock.json. Do not push.",
            project="FinaSync",
            autonomy="local",
        )
    assert result["task_contract"]["authority_resolution"]["result"] == "AUTHORIZED"
    assert "REMOTE_PUSH" in result["task_contract"]["authority_resolution"]["prohibited_actions"]
    run_go.assert_called_once()


def test_high_risk_low_complexity_does_not_bypass_authority():
    with patch("nexus.intake.run_go") as run_go:
        result = dispatch_request(
            "Update config in app.py for a production migration.",
            project="FinaSync",
        )
    assert result["result"]["status"] == "NEEDS_APPROVAL"
    run_go.assert_not_called()


def test_program_preflight_happens_before_program_runner():
    with patch(
        "nexus.intake.start_program",
        return_value={
            "program_id": "PROGRAM-LOCAL",
            "status": "SPECIFIED",
            "path": "C:/product",
        },
    ) as program:
        result = dispatch_request(
            "Create this complete local project fully autonomous. "
            "Run tests and fix errors. Do not deploy or push.",
            autonomy="local",
            name="Product",
        )
    assert result["execution_mode"] == "PROGRAM"
    assert result["task_contract"]["capability_resolution"]["all_supported"] is True
    assert result["task_contract"]["authority_resolution"]["result"] == "AUTHORIZED"
    program.assert_called_once()


def test_program_with_required_deployment_is_blocked_before_architect():
    with patch("nexus.intake.start_program") as program:
        result = dispatch_request(
            "Create this complete project and deploy it to production.",
            name="Product",
        )
    assert result["result"]["status"] == "UNSUPPORTED_CAPABILITY"
    assert "EXTERNAL_DEPLOYMENT" in result["result"]["missing"]
    program.assert_not_called()
