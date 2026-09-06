"""Read-only operational projection over the real SQLite runs/agents registry.

This module never writes to nexus.tasks.registry (the in-memory Mission
Board projection) and never mutates any registry data. It exposes the real
Runs and Agents created by the Nexus CLI ("nexus go") orchestration flow --
Manager, Worker, and ManagerReview agents recorded in nexus.registry.runs /
nexus.registry.agents -- so operational executions are visible in the web
UI without touching the existing Mission/Task Board projection at all.
"""

import json

from nexus.registry import agents as agents_registry
from nexus.registry import runs as runs_registry


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def _decode_reviewer_routing(branch):
    """Decode reviewer routing metadata persisted in the agent's branch field.

    ManagerReview agents never use \x27branch\x27 for source-control branches
    (that field is only meaningful for Worker agents), so it is reused,
    unmodified in schema, to persist the adaptive reviewer routing decision
    (reason, degraded, execution_path) recorded at review dispatch time.
    Returns None when absent or not decodable, never raising.
    """
    if not branch:
        return None
    try:
        decoded = json.loads(branch)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def get_operational_runs():
    """Return every real Run recorded in the SQLite registry."""
    return [_row_to_dict(row) for row in runs_registry.list_runs()]


def get_operational_run(run_id):
    """Return a single real Run, or None when it does not exist."""
    return _row_to_dict(runs_registry.get_run(run_id))


def get_operational_agents(run_id=None):
    """Return real Agents (Manager/Worker/ManagerReview) from the registry.

    When \x27run_id\x27 is provided, scopes to that Run\x27s Agents (Mission/Task
    Board non-regression: this never touches nexus.tasks.registry). Each
    ManagerReview entry additionally carries a decoded \x27reviewer_routing\x27
    payload when routing metadata was persisted for it.
    """
    if run_id is not None:
        rows = agents_registry.list_agents_for_run(run_id)
    else:
        rows = agents_registry.list_agents()

    result = []
    for row in rows:
        item = _row_to_dict(row)
        if item.get("role") == "ManagerReview":
            item["reviewer_routing"] = _decode_reviewer_routing(
                item.get("branch")
            )
        result.append(item)

    return result


def get_operational_run_detail(run_id):
    """Return a Run with its full real Agent lineage, or None if unknown."""
    run = get_operational_run(run_id)
    if run is None:
        return None

    run["agents"] = get_operational_agents(run_id=run_id)
    return run


def get_reviewer_routing_history(run_id=None):
    """Return the ManagerReview routing history for real Runs.

    Each entry describes one ManagerReview agent: its identity, the Worker
    it reviewed (\x27parent_agent_id\x27), and its decoded reviewer routing
    metadata (model/provider/effort/execution_path/reason/degraded) when
    persisted.
    """
    agents = get_operational_agents(run_id=run_id)

    history = []
    for agent in agents:
        if agent.get("role") != "ManagerReview":
            continue
        history.append(
            {
                "reviewer_id": agent.get("id"),
                "run_id": agent.get("run_id"),
                "worker_id": agent.get("parent_agent_id"),
                "model": agent.get("model"),
                "provider": agent.get("provider"),
                "effort": agent.get("effort"),
                "status": agent.get("status"),
                "routing": agent.get("reviewer_routing"),
            }
        )

    return history
