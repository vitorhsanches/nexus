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
