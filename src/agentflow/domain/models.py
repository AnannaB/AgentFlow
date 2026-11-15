"""Strict Pydantic domain models, with no persistence dependencies."""

import json
import re
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agentflow.domain.states import (
    ApprovalStatus,
    AttemptStatus,
    PlannerKind,
    RunStatus,
    StepStatus,
)

_SENSITIVE_DETAIL_KEY = re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)")
TOOL_RESULT_MAX_BYTES = 256 * 1024


def _validate_json_size(value: JsonValue, field_name: str, max_bytes: int) -> JsonValue:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must contain finite JSON values") from error
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field_name} exceeds the {max_bytes}-byte limit")
    return value


def _reject_sensitive_keys(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _SENSITIVE_DETAIL_KEY.search(key):
                raise ValueError("event details must not contain sensitive credential fields")
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested)


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Shared strict and immutable configuration for domain values."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        frozen=True,
    )

    @model_validator(mode="after")
    def timestamps_are_utc(self) -> Self:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.utcoffset() is None or value.utcoffset().total_seconds() != 0
            ):
                raise ValueError(f"{field_name} must be timezone-aware UTC")
        return self


class PlanStep(DomainModel):
    """A proposed step in a structured execution plan."""

    step_key: str = Field(min_length=1, max_length=100)
    tool_name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=1000)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    required: bool = True
    risk_level: str = Field(default="low", pattern=r"^(low|medium|high)$")

    @model_validator(mode="after")
    def arguments_are_bounded_json(self) -> Self:
        _validate_json_size(self.arguments, "arguments", 64 * 1024)
        return self


class ExecutionPlan(DomainModel):
    """Validated plan proposal; it contains data, never executable code."""

    objective_summary: str = Field(min_length=1, max_length=2000)
    steps: list[PlanStep] = Field(min_length=1, max_length=100)
    completion_criteria: list[str] = Field(default_factory=list, max_length=100)
    requires_approval: bool = False

    @model_validator(mode="after")
    def dependencies_are_valid(self) -> Self:
        keys = [step.step_key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("step_key values must be unique")
        key_set = set(keys)
        for step in self.steps:
            if step.step_key in step.depends_on:
                raise ValueError(f"step {step.step_key!r} cannot depend on itself")
            unknown = set(step.depends_on) - key_set
            if unknown:
                raise ValueError(
                    f"step {step.step_key!r} has unknown dependencies: {sorted(unknown)}"
                )
            if len(step.depends_on) != len(set(step.depends_on)):
                raise ValueError(f"step {step.step_key!r} has duplicate dependencies")

        dependencies = {step.step_key: step.depends_on for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_key: str) -> None:
            if step_key in visiting:
                raise ValueError("plan dependencies must not contain a cycle")
            if step_key in visited:
                return
            visiting.add(step_key)
            for dependency in dependencies[step_key]:
                visit(dependency)
            visiting.remove(step_key)
            visited.add(step_key)

        for step_key in dependencies:
            visit(step_key)
        return self


class ToolResult(DomainModel):
    """Validated data returned by a registered tool."""

    success: bool
    data: dict[str, JsonValue] = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=4000)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def data_is_bounded_json(self) -> Self:
        _validate_json_size(self.data, "data", TOOL_RESULT_MAX_BYTES)
        return self


class WorkflowRun(DomainModel):
    """Persistent workflow run metadata and current plan/state."""

    id: UUID = Field(default_factory=uuid4)
    objective: str = Field(min_length=1, max_length=4000)
    status: RunStatus = RunStatus.QUEUED
    planner_kind: PlannerKind = PlannerKind.RULE_BASED
    plan: ExecutionPlan | None = None
    source_run_id: UUID | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_summary: str | None = Field(default=None, max_length=4000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowStep(DomainModel):
    """Persisted executable step belonging to a workflow run."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_key: str = Field(min_length=1, max_length=100)
    position: int = Field(ge=0)
    tool_name: str = Field(min_length=1, max_length=100)
    tool_version: str | None = Field(default=None, min_length=1, max_length=64)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    required: bool = True
    approval_required: bool = False
    status: StepStatus = StepStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def arguments_are_bounded_json(self) -> Self:
        _validate_json_size(self.arguments, "arguments", 64 * 1024)
        return self


class StepAttempt(DomainModel):
    """One persisted attempt to execute a workflow step."""

    id: UUID = Field(default_factory=uuid4)
    step_id: UUID
    attempt_number: int = Field(ge=1)
    status: AttemptStatus = AttemptStatus.RUNNING
    input_hash: str | None = Field(default=None, min_length=1, max_length=128)
    output: ToolResult | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_summary: str | None = Field(default=None, max_length=4000)
    duration_ms: int | None = Field(default=None, ge=0)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class ApprovalRequest(DomainModel):
    """Persisted plan-level or step-level human approval request."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_id: UUID | None = None
    purpose: Literal["workflow", "report_export"] = "workflow"
    status: ApprovalStatus = ApprovalStatus.PENDING
    plan_digest: str = Field(min_length=1, max_length=128)
    decision_note: str | None = Field(default=None, max_length=4000)
    requested_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class RunEvent(DomainModel):
    """Sanitized event associated with a workflow run."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    event_type: str = Field(min_length=1, max_length=100)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def details_are_bounded_and_sanitized(self) -> Self:
        _validate_json_size(self.details, "details", 16 * 1024)
        _reject_sensitive_keys(self.details)
        return self


class WorkflowTemplate(DomainModel):
    """Reusable validated plan definition, separate from execution history."""

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    plan: ExecutionPlan
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    archived_at: datetime | None = None
