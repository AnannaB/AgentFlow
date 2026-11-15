"""Pydantic result contract for natural-language planning."""

from pydantic import Field

from agentflow.domain.models import DomainModel, ExecutionPlan
from agentflow.domain.states import PlannerKind


class PlanningResult(DomainModel):
    """Validated plan plus transparent provenance and diagnostics."""

    objective: str = Field(min_length=1, max_length=4000)
    plan: ExecutionPlan
    planner_kind: PlannerKind
    model: str | None = Field(default=None, max_length=200)
    fallback_reason: str | None = Field(default=None, max_length=100)
    duration_ms: int = Field(ge=0)
