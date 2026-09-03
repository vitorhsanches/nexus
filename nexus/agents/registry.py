"""In-memory Agent Registry for the Nexus Agent Execution Loop V1."""

from nexus.agents.lifecycle import transition
from nexus.agents.models import Agent


class AgentNotFoundError(KeyError):
    pass


class NoAvailableAgentError(RuntimeError):
    pass


class AgentRegistry:
    """Stores agents in memory and answers lookup/availability queries."""

    def __init__(self):
        self._agents = {}

    def register_agent(self, agent):
        """Register an agent, returning the registered agent."""
        if not isinstance(agent, Agent):
            agent = Agent(**agent)
        self._agents[agent.agent_id] = agent
        return agent

    def get_agent(self, agent_id):
        try:
            return self._agents[agent_id]
        except KeyError:
            raise AgentNotFoundError(agent_id)

    def list_agents(self):
        return list(self._agents.values())

    def find_available_agent(self, capability=None):
        """Return an AVAILABLE agent, optionally requiring a capability.

        Raises NoAvailableAgentError when no matching agent is found.
        """
        for agent in self._agents.values():
            if agent.status != "AVAILABLE":
                continue
            if capability is not None and capability not in (agent.capabilities or []):
                continue
            return agent
        raise NoAvailableAgentError(
            f"No available agent" + (f" with capability {capability!r}" if capability else "")
        )

    def update_agent_status(self, agent_id, status):
        agent = self.get_agent(agent_id)
        agent.status = transition(agent.status, status)
        return agent
