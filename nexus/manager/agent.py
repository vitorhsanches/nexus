"""Manager Agent abstraction for the Nexus Manager Agent Planning Engine V1.

The Manager is the first agent in the Nexus pipeline: it receives a Mission,
analyzes its title and description through the deterministic planner, turns the
resulting plan into real tasks via the existing Mission Engine + Task Registry,
and exposes them on the Nexus Mission Board.

No existing execution flow is changed: the Manager only adds new entry points
that reuse the Mission Engine, the Task Registry, the Intent Router, and the
Capability Router. Tasks produced here carry ``required_capabilities`` so the
Agent Capability Router can assign the correct agent later.
"""

from dataclasses import dataclass, field
from typing import Optional

from nexus.board import service as board_service
from nexus.missions import service as mission_service
from nexus.missions.models import Mission
from nexus.tasks.models import Task


@dataclass(slots=True)
class ManagerResult:
    """Outcome of a Manager planning run."""

    manager_id: str
    mission: Optional[Mission] = None
    tasks: list = field(default_factory=list)
    intent: Optional[str] = None
    board_seeded: bool = False


class ManagerError(RuntimeError):
    """Raised when a Manager cannot materialize a plan."""


class ManagerAgent:
    """Transforms a Mission into executable tasks through the Mission Engine."""

    def __init__(self, manager_id: str = "manager-agent", planner=None):
        self.manager_id = manager_id
        # Injected for deterministic reuse; imported lazily to keep this module
        # side-effect free at import time.
        if planner is None:
            from nexus.manager.planner import build_execution_plan

            planner = build_execution_plan
        self._plan_mission = planner

    def plan(self, mission) -> "ExecutionPlan":
        """Analyze a Mission and return its deterministic ExecutionPlan."""
        return self._plan_mission(mission)

    def execute(self, mission) -> ManagerResult:
        """Plan a Mission and materialize its tasks (manager run).

        The tasks are created through the existing Mission Engine and Task
        Registry, so they are immediately visible to every consumer of those
        stores, including the Nexus Mission Board web views.
        """
        plan = self._plan_mission(mission)

        if isinstance(mission, dict):
            run_id = mission.get("run_id") or "RUN-MANAGER"
            project_id = mission.get("project_id")
            execution_path = mission.get("execution_path")
        else:
            run_id = getattr(mission, "run_id", None) or "RUN-MANAGER"
            project_id = getattr(mission, "project_id", None)
            execution_path = getattr(mission, "execution_path", None)

        materialized_mission = self._materialize(
            plan, run_id, project_id=project_id, execution_path=execution_path
        )

        return ManagerResult(
            manager_id=self.manager_id,
            mission=materialized_mission,
            tasks=list(materialized_mission.tasks),
            intent=plan.intent,
        )

    def plan_and_create(self, mission) -> "Mission":
        """Plan a Mission and return the created Mission with its tasks."""
        return self.execute(mission).mission

    def _materialize(
        self, plan, run_id: str, project_id=None, execution_path=None
    ) -> Mission:
        """Create a Mission and its tasks through the Mission Engine.

        The planner only decides *what* work is needed; orchestration
        context (project_id/execution_path) decides *where/how* it may
        execute and is attached here, separately from semantic planning.
        """
        payload = plan.to_dict()

        if project_id:
            payload["project_id"] = project_id

        if execution_path:
            payload["execution_path"] = execution_path

        try:
            mission = mission_service.create_mission_from_plan(
                plan=payload,
                run_id=run_id,
            )
        except Exception as error:  # noqa: BLE001 - wrap any store failure.
            raise ManagerError(f"Failed to materialize plan: {error}") from error

        seeded = False
        if mission.tasks:
            try:
                board_service.create_board(mission)
                seeded = True
            except Exception:  # noqa: BLE001 - board seeding must not break planning.
                seeded = False

        return mission

    def select_agent_for_task(self, task: Task, agent_registry=None):
        """Route a task to its required agent via the Capability Router.

        Reuses the existing Agent Capability Router so generated tasks keep
        flowing through the current execution path unchanged.
        """
        from nexus.capabilities.router import select_agent_for_capabilities

        required = _required_capabilities(task)
        return select_agent_for_capabilities(
            required_capabilities=required,
            agent_registry=agent_registry,
        )


def _required_capabilities(task: Task) -> list[str]:
    """Read required capabilities from a task's execution policy."""
    policy = task.execution_policy
    if not isinstance(policy, dict):
        return []
    return list(policy.get("required_capabilities") or [])


def run(mission) -> ManagerResult:
    """Convenience entry point: run the default Manager against a Mission."""
    manager = ManagerAgent()
    return manager.execute(mission)
