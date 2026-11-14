"""Validation and safe-default coverage for domain models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentflow.domain.models import (
    ApprovalRequest,
    ExecutionPlan,
    PlanStep,
    RunEvent,
    StepAttempt,
    ToolResult,
    WorkflowRun,
    WorkflowStep,
    WorkflowTemplate,
)


def sample_plan() -> ExecutionPlan:
    return ExecutionPlan(
        objective_summary="Summarize a report",
        steps=[PlanStep(step_key="read", tool_name="read_report", purpose="Read report")],
    )


def test_domain_models_use_independent_safe_defaults() -> None:
    first = WorkflowRun(objective="First")
    second = WorkflowRun(objective="Second")

    assert first.id != second.id
    assert first.status.value == "queued"
    assert first.created_at.tzinfo == UTC

    result_a = ToolResult(success=True, summary="done")
    result_b = ToolResult(success=True, summary="done")
    plan_step_a = PlanStep(step_key="a", tool_name="tool", purpose="purpose")
    plan_step_b = PlanStep(step_key="b", tool_name="tool", purpose="purpose")
    assert result_a.data == result_b.data == {}
    assert result_a.data is not result_b.data
    assert result_a.warnings is not result_b.warnings
    assert plan_step_a.arguments is not plan_step_b.arguments
    assert plan_step_a.depends_on is not plan_step_b.depends_on


def test_model_rejects_extra_fields_and_coercion() -> None:
    with pytest.raises(ValidationError):
        WorkflowRun(objective="valid", surprise="not allowed")
    with pytest.raises(ValidationError):
        WorkflowStep(
            run_id=WorkflowRun(objective="run").id, step_key="s", position=True, tool_name="x"
        )


def test_model_requires_timezone_aware_utc_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        WorkflowRun(objective="x", created_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        WorkflowRun(objective="x", created_at=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None))


@pytest.mark.parametrize(
    "steps",
    [
        [
            PlanStep(step_key="same", tool_name="a", purpose="a"),
            PlanStep(step_key="same", tool_name="b", purpose="b"),
        ],
        [PlanStep(step_key="a", tool_name="a", purpose="a", depends_on=["missing"])],
        [PlanStep(step_key="a", tool_name="a", purpose="a", depends_on=["a"])],
        [
            PlanStep(step_key="a", tool_name="a", purpose="a", depends_on=["b"]),
            PlanStep(step_key="b", tool_name="b", purpose="b", depends_on=["a"]),
        ],
    ],
)
def test_execution_plan_rejects_invalid_dependency_graph(steps: list[PlanStep]) -> None:
    with pytest.raises(ValidationError):
        ExecutionPlan(objective_summary="invalid graph", steps=steps)


def test_all_requested_entities_validate_and_link_as_domain_data() -> None:
    run = WorkflowRun(objective="Analyze a report", plan=sample_plan())
    step = WorkflowStep(run_id=run.id, step_key="read", position=0, tool_name="read_report")
    attempt = StepAttempt(
        step_id=step.id, attempt_number=1, output=ToolResult(success=True, summary="read")
    )
    approval = ApprovalRequest(run_id=run.id, plan_digest="digest")
    event = RunEvent(run_id=run.id, event_type="created")
    template = WorkflowTemplate(name="Report summary", plan=sample_plan())

    assert attempt.output is not None
    assert approval.step_id is None
    assert event.details == {}
    assert template.version == 1
    assert datetime.now(UTC) >= run.created_at


@pytest.mark.parametrize(
    "details",
    [
        {"api_key": "must not persist"},
        {"safe": {"authorization": "Bearer value"}},
        {"oversized": "x" * (16 * 1024)},
        {"not_finite": float("nan")},
    ],
)
def test_run_events_reject_sensitive_or_unbounded_details(details: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunEvent(run_id=WorkflowRun(objective="event").id, event_type="test", details=details)
