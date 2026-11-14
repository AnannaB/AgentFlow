"""Validated return models for workflow execution services."""

from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue

from agentflow.domain.models import DomainModel, RunEvent
from agentflow.domain.states import RunStatus, StepStatus


class FailureDetail(DomainModel):
    """Sanitized failure summary; never contains raw tool inputs or exception text."""

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)
    step_key: str | None = Field(default=None, max_length=100)
    attempt_number: int | None = Field(default=None, ge=1)


class StepExecutionResult(DomainModel):
    """Final view of one step, including its persisted output when successful."""

    step_key: str = Field(min_length=1, max_length=100)
    tool_name: str = Field(min_length=1, max_length=100)
    status: StepStatus
    attempts: int = Field(ge=0)
    output: dict[str, JsonValue] | None = None
    failure: FailureDetail | None = None
    duration_ms: int = Field(ge=0)


class ExecutionResult(DomainModel):
    """Bounded result returned by a workflow execution or approval pause."""

    run_id: UUID
    status: RunStatus
    steps: list[StepExecutionResult] = Field(max_length=100)
    failure: FailureDetail | None = None
    duration_ms: int = Field(ge=0)
    events: list[RunEvent] = Field(default_factory=list, max_length=500)

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True, frozen=True)
