from __future__ import annotations

from threading import Lock
from time import monotonic

_LOCK = Lock()
_RUNS: dict[str, dict] = {}


def start_run(
    run_id: str,
    execution_strategy: str | None = None,
    *,
    task_type: str | None = None,
    execution_mode: str | None = None,
    required_capabilities: tuple[str, ...] = (),
    unsupported_capabilities: tuple[str, ...] = (),
    authority_result: str | None = None,
    autonomy_profile: str | None = None,
) -> None:
    with _LOCK:
        _RUNS[run_id] = {
            "execution_strategy": execution_strategy,
            "task_type": task_type,
            "execution_mode": execution_mode,
            "required_capabilities": list(required_capabilities),
            "unsupported_capabilities": list(unsupported_capabilities),
            "authority_result": authority_result,
            "autonomy_profile": autonomy_profile,
            "started_monotonic": monotonic(),
            "calls": [],
        }


def record_call(
    run_id: str,
    *,
    role: str,
    model: str | None = None,
    provider: str | None = None,
    capability: str | None = None,
    route_class: str | None = None,
    execution_strategy: str | None = None,
    usage: dict | None = None,
    latency: float | None = None,
    attempt_number: int | None = None,
    reason: str = "INITIAL",
    result: str | None = None,
    estimated_cost: float | None = None,
) -> None:
    """Record best-effort call metadata; telemetry must never raise."""
    try:
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if total_tokens is None and isinstance(input_tokens, int) and isinstance(output_tokens, int):
            total_tokens = input_tokens + output_tokens
        with _LOCK:
            run = _RUNS.setdefault(run_id, {"execution_strategy": execution_strategy, "started_monotonic": monotonic(), "calls": []})
            if reason == "RETRY" and run["calls"] and model and run["calls"][-1].get("model") != model:
                reason = "ESCALATION"
            run["calls"].append({
                "role": role, "model": model, "provider": provider,
                "capability": capability, "route_class": route_class,
                "execution_strategy": execution_strategy or run.get("execution_strategy"),
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": total_tokens, "latency": latency,
                "attempt_number": attempt_number, "reason": reason,
                "result": result, "estimated_cost": estimated_cost,
            })
    except Exception:
        return


def get_run_summary(run_id: str) -> dict:
    with _LOCK:
        run = _RUNS.get(run_id, {"execution_strategy": None, "started_monotonic": monotonic(), "calls": []})
        calls = list(run["calls"])
    known_total = [c["total_tokens"] for c in calls if isinstance(c["total_tokens"], int)]
    return {
        "llm_calls": len(calls),
        "input_tokens": sum(c["input_tokens"] for c in calls if isinstance(c["input_tokens"], int)) or None,
        "output_tokens": sum(c["output_tokens"] for c in calls if isinstance(c["output_tokens"], int)) or None,
        "total_tokens": sum(known_total) if known_total else None,
        "providers_used": sorted({c["provider"] for c in calls if c["provider"]}),
        "models_used": sorted({c["model"] for c in calls if c["model"]}),
        "execution_strategy": run.get("execution_strategy"),
        "task_type": run.get("task_type"),
        "execution_mode": run.get("execution_mode"),
        "required_capabilities": run.get("required_capabilities", []),
        "unsupported_capabilities": run.get("unsupported_capabilities", []),
        "authority_result": run.get("authority_result"),
        "autonomy_profile": run.get("autonomy_profile"),
        "retry_count": sum(c["reason"] == "RETRY" for c in calls),
        "fallback_count": sum(c["reason"] == "FALLBACK" for c in calls),
        "escalation_count": sum(c["reason"] == "ESCALATION" for c in calls),
        "estimated_cost": sum(c["estimated_cost"] for c in calls if isinstance(c["estimated_cost"], (int, float))) if calls and all(c["estimated_cost"] is not None for c in calls) else None,
        "calls": calls,
    }
