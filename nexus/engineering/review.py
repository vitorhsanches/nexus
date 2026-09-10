"""Deterministic approval decisions for engineering execution proposals."""

from dataclasses import dataclass, fields
from enum import Enum
import json
from typing import Any

from .errors import EngineeringProposalValidationError


class ReviewDecisionStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def _text(value: Any, name: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise EngineeringProposalValidationError(f"{name} must be a non-empty string")
    return value


def _scope(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EngineeringProposalValidationError("approval_scope must contain non-empty strings")
    return tuple(sorted(set(value)))


@dataclass(frozen=True, slots=True)
class EngineeringReviewDecision:
    review_id: str
    execution_id: str
    decision: ReviewDecisionStatus
    reviewer: str
    comments: str
    approval_scope: tuple[str, ...]
    created_at: str
    reviewed_at: str

    def __post_init__(self) -> None:
        _text(self.review_id, "review_id")
        _text(self.execution_id, "execution_id")
        _text(self.created_at, "created_at")
        _text(self.reviewer, "reviewer", required=False)
        _text(self.comments, "comments", required=False)
        _text(self.reviewed_at, "reviewed_at", required=False)
        decision = self.decision
        if isinstance(decision, str):
            try:
                decision = ReviewDecisionStatus(decision)
            except ValueError as exc:
                raise EngineeringProposalValidationError("decision must be ReviewDecisionStatus") from exc
            object.__setattr__(self, "decision", decision)
        elif not isinstance(decision, ReviewDecisionStatus):
            raise EngineeringProposalValidationError("decision must be ReviewDecisionStatus")
        object.__setattr__(self, "approval_scope", _scope(self.approval_scope))
        if decision is ReviewDecisionStatus.APPROVED:
            if not self.reviewer:
                raise EngineeringProposalValidationError("approved review requires reviewer")
            if not self.approval_scope:
                raise EngineeringProposalValidationError("approved review requires approval_scope")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: (value.value if isinstance(value, Enum) else list(value) if isinstance(value, tuple) else value)
                for field in fields(self) for value in (getattr(self, field.name),)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def create_pending_review(execution_id: str, review_id: str, created_at: str,
                          comments: str = "") -> EngineeringReviewDecision:
    return EngineeringReviewDecision(review_id, execution_id, ReviewDecisionStatus.PENDING,
                                     "", comments, (), created_at, "")


def approve_execution(review: EngineeringReviewDecision, reviewer: str,
                      approval_scope: tuple[str, ...] | list[str] | str,
                      comments: str = "", reviewed_at: str = "") -> EngineeringReviewDecision:
    if not isinstance(review, EngineeringReviewDecision):
        raise EngineeringProposalValidationError("missing review")
    return EngineeringReviewDecision(review.review_id, review.execution_id,
                                     ReviewDecisionStatus.APPROVED, reviewer, comments,
                                     approval_scope, review.created_at, reviewed_at)


def reject_execution(review: EngineeringReviewDecision, reviewer: str = "",
                     comments: str = "", reviewed_at: str = "") -> EngineeringReviewDecision:
    if not isinstance(review, EngineeringReviewDecision):
        raise EngineeringProposalValidationError("missing review")
    return EngineeringReviewDecision(review.review_id, review.execution_id,
                                     ReviewDecisionStatus.REJECTED, reviewer, comments,
                                     (), review.created_at, reviewed_at)
