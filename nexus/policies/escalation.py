from copy import deepcopy


MAX_SAME_TIER_RETRIES = 1


class EscalationUnavailable(RuntimeError):
    pass


ROUTE_LADDERS = {
    "mechanical": [
        {
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
        },
        {
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
        },
    ],
    "standard-coding": [
        {
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
        }
    ],
}


MECHANICAL_CAPABILITIES = {
    "mechanical",
    "formatting",
    "cleanup",
}


class RouteOverrideValidationError(ValueError):
    """Raised when a runtime route override violates approved policy."""


def route_class_for_policy(execution_policy) -> str:
    """Resolve the approved route class for an execution policy."""
    policy = (
        execution_policy
        if isinstance(execution_policy, dict)
        else {}
    )

    explicit = policy.get("route_class")
    if explicit in ROUTE_LADDERS:
        return explicit

    required = set(
        policy.get("required_capabilities") or []
    )

    if (
        required
        and required.issubset(MECHANICAL_CAPABILITIES)
    ):
        return "mechanical"

    return "standard-coding"


def validate_route_for_class(
    route_class: str,
    route_override,
) -> dict:
    """Validate model/effort against one approved capability ladder."""
    if route_class not in ROUTE_LADDERS:
        raise RouteOverrideValidationError(
            f"Unknown or missing route_class: {route_class!r}."
        )

    if not isinstance(route_override, dict):
        raise RouteOverrideValidationError(
            "route_override must be a dict."
        )

    requested_class = route_override.get("route_class")
    if (
        requested_class is not None
        and requested_class != route_class
    ):
        raise RouteOverrideValidationError(
            f"route_override route_class {requested_class!r} "
            f"does not match approved class {route_class!r}."
        )

    model = route_override.get("model")
    effort = route_override.get("effort", "low")

    if not isinstance(model, str) or not model.strip():
        raise RouteOverrideValidationError(
            "route_override model is missing."
        )

    if not isinstance(effort, str) or not effort.strip():
        raise RouteOverrideValidationError(
            "route_override effort is missing."
        )

    for approved in ROUTE_LADDERS[route_class]:
        if (
            approved["model"] == model
            and approved["effort"] == effort
        ):
            return {
                "route_class": route_class,
                "model": model,
                "effort": effort,
            }

    raise RouteOverrideValidationError(
        f"Route override model={model!r}, effort={effort!r} "
        f"is not approved for route_class={route_class!r}."
    )


def validate_route_override(
    execution_policy,
    route_override,
):
    """Validate a runtime override against the Task's approved ladder."""
    if route_override is None:
        return None

    route_class = route_class_for_policy(
        execution_policy
    )

    return validate_route_for_class(
        route_class,
        route_override,
    )


def _route_identity(route: dict) -> tuple[str, str]:
    return (
        route["execution_path"],
        route["model"],
    )


def next_route(worker: dict) -> dict:
    route_class = worker["route_class"]
    ladder = ROUTE_LADDERS.get(route_class)

    if not ladder:
        raise EscalationUnavailable(
            f"No approved escalation ladder for "
            f"route_class={route_class!r}."
        )

    current_identity = _route_identity(worker)

    for index, route in enumerate(ladder):
        if _route_identity(route) != current_identity:
            continue

        next_index = index + 1

        if next_index >= len(ladder):
            raise EscalationUnavailable(
                f"No stronger approved route after "
                f"{worker['model']!r} for "
                f"{route_class!r}."
            )

        escalated = deepcopy(worker)
        escalated.update(ladder[next_index])

        return escalated

    raise EscalationUnavailable(
        f"Current route {current_identity!r} is not present "
        f"in the approved {route_class!r} ladder."
    )


def failure_context(review: dict) -> str:
    evidence = review.get("evidence") or []

    evidence_text = "\n".join(
        f"- {item}"
        for item in evidence
    )

    return f"""
Previous Worker review:

Verdict:
{review["verdict"]}

Failure class:
{review.get("failure_class")}

Review summary:
{review["summary"]}

Evidence:
{evidence_text}

Use this diagnostic context to avoid repeating the previous failure.
""".strip()
