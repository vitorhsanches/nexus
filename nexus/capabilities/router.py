"""Capability-based agent selection for the Nexus Agent Capability Router V1."""

from nexus.agents.registry import AgentRegistry, NoAvailableAgentError

# Re-exported so callers can depend on the capability routing layer without
# reaching into the agent registry for the rejection error.
NoSuitableAgentError = NoAvailableAgentError


def _available_agents(agent_registry):
    return [
        agent
        for agent in agent_registry.list_agents()
        if agent.status == "AVAILABLE"
    ]


def _covers(agent_capabilities, required):
    """Return True when an agent's capabilities include every required one."""
    agent_set = set(agent_capabilities or [])
    if not required:
        return True
    return required.issubset(agent_set)


def select_agent_for_capabilities(required_capabilities, agent_registry=None):
    """Return the best AVAILABLE agent that satisfies the required capabilities.

    Inspects the registered agents from ``nexus.agents.registry``, keeps only
    those that are AVAILABLE and can cover every required capability, and
    returns the tightest match (fewest extra capabilities). Raises
    ``NoSuitableAgentError`` when no suitable agent exists.
    """
    if agent_registry is None:
        agent_registry = AgentRegistry()

    if required_capabilities is None:
        required_capabilities = []
    required = set(required_capabilities)

    candidates = [
        agent
        for agent in _available_agents(agent_registry)
        if _covers(agent.capabilities, required)
    ]

    if not candidates:
        detail = (
            f" with all required capabilities {sorted(required)!r}"
            if required
            else ""
        )
        raise NoSuitableAgentError(f"No suitable agent{detail}.")

    # The most specific match wins: the agent that covers the required set with
    # the fewest extra capabilities. Exact matches are therefore preferred, and
    # ties keep stable registration order (``min`` is stable).
    def extra_count(agent):
        return len(agent.capabilities or [])

    return min(candidates, key=extra_count)
