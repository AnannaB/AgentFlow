"""Orchestrate local LLM planning, validation, and explicit fallback behavior."""

from __future__ import annotations

import logging
import time

from agentflow.application.execution import WorkflowExecutionService
from agentflow.application.models import ExecutionResult
from agentflow.config import Settings, get_settings
from agentflow.domain.models import WorkflowRun
from agentflow.domain.states import PlannerKind
from agentflow.planning.errors import OllamaConnectionError, PlanningError, PlanValidationError
from agentflow.planning.fallback import RuleBasedPlanner
from agentflow.planning.models import PlanningResult
from agentflow.planning.ollama import OllamaClient
from agentflow.planning.validation import PlanValidator
from agentflow.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class PlanningService:
    """Generate a validated plan and require an explicit call to create its run."""

    def __init__(
        self,
        registry: ToolRegistry,
        settings: Settings | None = None,
        *,
        client: OllamaClient | None = None,
        fallback: RuleBasedPlanner | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings or get_settings()
        self._client = client or OllamaClient(self._settings)
        self._fallback = fallback or RuleBasedPlanner(registry)
        self._validator = validator or PlanValidator(registry)

    def plan(self, objective: str) -> PlanningResult:
        """Return validated plan and provenance; never execute it implicitly."""
        objective = objective.strip()
        if not objective:
            raise PlanValidationError("Objective must not be empty.")
        if len(objective) > 4000:
            raise PlanValidationError("Objective exceeds the 4000-character limit.")

        started = time.monotonic()
        try:
            response = self._client.generate_plan(objective, self._registry.discover())
            plan = self._validator.validate(response)
            return PlanningResult(
                objective=objective,
                plan=plan,
                planner_kind=PlannerKind.OLLAMA,
                model=self._client.model,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except PlanningError as error:
            if not self._settings.ollama_fallback_enabled:
                raise
            reason = _fallback_code(error)
            logger.warning("Using rule-based planning fallback: reason=%s", reason)
            fallback_plan = self._validator.validate(self._fallback.plan(objective))
            return PlanningResult(
                objective=objective,
                plan=fallback_plan,
                planner_kind=PlannerKind.RULE_BASED,
                fallback_reason=reason,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )

    def create_run(
        self, result: PlanningResult, execution_service: WorkflowExecutionService
    ) -> WorkflowRun:
        """Persist this validated plan through the existing execution service."""
        plan = self._validator.validate(result.plan)
        return execution_service.create_run(
            plan,
            objective=result.objective,
            planner_kind=result.planner_kind,
        )

    def execute(
        self,
        result: PlanningResult,
        execution_service: WorkflowExecutionService,
        *,
        workspace_roots: tuple[str, ...] = (),
    ) -> ExecutionResult:
        """Explicitly create and execute a run from a previously planned result."""
        run = self.create_run(result, execution_service)
        return execution_service.execute(run.id, workspace_roots=workspace_roots)


def _fallback_code(error: PlanningError) -> str:
    if isinstance(error, OllamaConnectionError):
        return "ollama_unavailable"
    if isinstance(error, PlanValidationError):
        return "invalid_ollama_plan"
    return "ollama_invalid_response"
