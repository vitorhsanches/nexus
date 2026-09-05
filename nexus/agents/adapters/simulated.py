"""Local/simulated execution adapter.

This is the default adapter used by ``AgentExecutor`` and by every existing
test: it performs no external calls and always reports success, matching the
previous hardcoded simulated behavior. This keeps default behavior safe so
tests never invoke external models.
"""

from nexus.agents.adapters.base import AdapterResult, ExecutionAdapter


class SimulatedAdapter(ExecutionAdapter):
    """No-op adapter that simulates a successful execution."""

    def run(self, context):
        return AdapterResult(
            success=True,
            output="Task executed successfully by agent",
            error=None,
        )
