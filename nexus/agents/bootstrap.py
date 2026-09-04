"""Default Agent Bootstrap for the Nexus Agent Execution Loop V1.

Provides ``initialize_default_agents`` which seeds the shared Agent Registry
with a small pool of default agents so tasks routed by the Capability Router
never start with an empty registry. It is idempotent: agents are registered
only when they do not already exist, and import time has no side effects.
"""

from nexus.agents.models import Agent
from nexus.web.agents import agent_registry

DEFAULT_AGENTS = (
    {
        "agent_id": "analysis-agent",
        "name": "Analysis Agent",
        "provider": "omniroute",
        "model": "analysis-agent",
        "capabilities": ["analysis", "requirements"],
    },
    {
        "agent_id": "architect-agent",
        "name": "Architect Agent",
        "provider": "omniroute",
        "model": "architect-agent",
        "capabilities": ["architecture", "design"],
    },
    {
        "agent_id": "developer-agent",
        "name": "Developer Agent",
        "provider": "omniroute",
        "model": "developer-agent",
        "capabilities": ["coding", "implementation"],
    },
)


def initialize_default_agents(agent_registry=None):
    """Register the default agents, skipping any that already exist.

    Returns the list of agents that were newly registered by this call.
    Idempotent: invoking it again registers nothing new.
    """
    if agent_registry is None:
        agent_registry = agent_registry_handle()

    created = []
    for definition in DEFAULT_AGENTS:
        if _has_agent(agent_registry, definition["agent_id"]):
            continue
        agent = Agent(
            agent_id=definition["agent_id"],
            name=definition["name"],
            provider=definition["provider"],
            model=definition["model"],
            capabilities=list(definition["capabilities"]),
        )
        agent_registry.register_agent(agent)
        created.append(agent)
    return created


def agent_registry_handle():
    """Return the process-wide Agent Registry shared by the web layer."""
    return agent_registry


def _has_agent(agent_registry, agent_id):
    try:
        agent_registry.get_agent(agent_id)
        return True
    except KeyError:
        return False
