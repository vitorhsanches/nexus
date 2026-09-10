"""Errors raised by the engineering context boundary."""


class EngineeringContextValidationError(ValueError):
    """Raised when an engineering context is incomplete or invalid."""


class EngineeringProposalValidationError(ValueError):
    """Raised when an engineering proposal is incomplete or invalid."""


class EngineeringPlanValidationError(ValueError):
    """Raised when an implementation plan is incomplete or invalid."""
