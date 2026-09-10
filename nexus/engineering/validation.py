"""Read-only validation for engineering proposals."""

from typing import Any

from .errors import EngineeringProposalValidationError


def validate_proposal(proposal: Any) -> None:
    """Validate the required proposal boundary without executing any work."""
    required_text = ("proposal_id", "context_id", "objective", "change_summary",
                     "rollback_plan", "risk_assessment", "created_at")
    for name in required_text:
        value = getattr(proposal, name, None)
        if not isinstance(value, str) or not value.strip():
            raise EngineeringProposalValidationError(f"{name} must be a non-empty string")
    if not getattr(proposal, "context_id", None):
        raise EngineeringProposalValidationError("missing context")
    for name, message in (("affected_files", "missing affected files"),
                          ("expected_tests", "missing validation plan")):
        value = getattr(proposal, name, None)
        if not isinstance(value, (tuple, list)) or not value:
            raise EngineeringProposalValidationError(message)

