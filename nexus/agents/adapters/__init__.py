"""Execution adapter abstraction for the Nexus Agent Execution Loop.

Package boundary: AgentExecutor stays the lifecycle orchestrator (task
lookup, capability routing, agent BUSY/AVAILABLE/FAILED transitions,
AgentSession creation/completion/failure, task lifecycle transitions). This
package only supplies the pluggable `ExecutionAdapter` implementations that
perform the actual unit of work delegated by the executor.
"""
