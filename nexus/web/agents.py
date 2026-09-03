"""Shared Agent Registry handle for the local web UI.

The built-in ``nexus.agents.registry`` module defines the AgentRegistry class
but no module-level instance. The execution loop instantiates its own
per-run registries, so this module provides a single, process-wide registry
handle that the read-only web views can inspect. Only read methods
(``list_agents``) are used by the web layer; no agent is ever written from
the UI.
"""

from nexus.agents.registry import AgentRegistry


agent_registry = AgentRegistry()