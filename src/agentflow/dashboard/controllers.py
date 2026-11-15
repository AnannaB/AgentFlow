"""Thin UI actions that preserve planning/execution service boundaries."""

from pathlib import Path
from uuid import UUID

from agentflow.application.execution import WorkflowExecutionService
from agentflow.application.models import ExecutionResult
from agentflow.domain.models import WorkflowRun
from agentflow.planning.models import PlanningResult
from agentflow.planning.service import PlanningService


def generate_plan(planner: PlanningService, objective: str) -> PlanningResult:
    """Request a validated plan without creating or executing a run."""
    return planner.plan(objective)


def create_workflow_run(
    planner: PlanningService,
    result: PlanningResult,
    execution: WorkflowExecutionService,
) -> WorkflowRun:
    """Persist a previously reviewed and validated planning result."""
    return planner.create_run(result, execution)


def execute_workflow(
    execution: WorkflowExecutionService,
    run_id: UUID,
    workspace: Path,
) -> ExecutionResult:
    """Execute a persisted run only after the user selects the run action."""
    return execution.execute(run_id, workspace_roots=(str(workspace),))
