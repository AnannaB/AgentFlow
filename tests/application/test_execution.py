"""End-to-end workflow execution tests using temporary SQLite and registered tools."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from agentflow.application.errors import RunNotExecutableError
from agentflow.application.execution import RetryPolicy, WorkflowExecutionService
from agentflow.domain.models import (
    ApprovalRequest,
    ExecutionPlan,
    PlanStep,
    StepAttempt,
)
from agentflow.domain.states import ApprovalStatus, AttemptStatus, RunStatus, StepStatus
from agentflow.storage.sqlite import SQLiteDatabase, SQLiteRepository
from agentflow.tools.base import ToolDefinition, ToolInput, ToolMetadata, ToolOutput, ToolRisk
from agentflow.tools.errors import (
    ToolInputError,
    ToolPermissionError,
    ToolTimeoutError,
    ToolTransientError,
)
from agentflow.tools.registry import ToolRegistry


class ValueInput(ToolInput):
    value: JsonValue | None = None


class ValueOutput(ToolOutput):
    value: JsonValue


@pytest.fixture
def service_factory(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "agentflow.sqlite3")
    database.initialize()
    repository = SQLiteRepository(database)
    registry = ToolRegistry()
    service = WorkflowExecutionService(
        repository,
        registry,
        retry_policy=RetryPolicy(max_retries=2, initial_backoff_seconds=0, max_backoff_seconds=0),
        wait=lambda delay, event: event.is_set(),
    )
    return repository, registry, service


def register_tool(
    registry,
    name,
    handler,
    *,
    version="1.0.0",
    input_model=ValueInput,
    output_model=ValueOutput,
    retryable=False,
    idempotent=True,
    requires_approval=False,
):
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name=name,
                version=version,
                description=f"Test implementation for {name}",
                risk=ToolRisk.HIGH if requires_approval else ToolRisk.LOW,
                timeout_seconds=2,
                retryable=retryable,
                idempotent=idempotent,
                requires_approval=requires_approval,
            ),
            input_model=input_model,
            output_model=output_model,
            handler=handler,
        )
    )


def plan(*steps: PlanStep, requires_approval=False):
    return ExecutionPlan(
        objective_summary="Execute a test workflow",
        steps=list(steps),
        requires_approval=requires_approval,
    )


def test_successful_single_step_persists_run_attempt_output_and_events(service_factory):
    repository, registry, service = service_factory
    register_tool(registry, "echo", lambda data, context: ValueOutput(value=data.value))
    run = service.create_run(
        plan(
            PlanStep(
                step_key="echo", tool_name="echo", purpose="Echo input", arguments={"value": 7}
            )
        )
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    assert result.steps[0].status == StepStatus.SUCCEEDED
    assert result.steps[0].output == {"value": 7}
    assert result.steps[0].attempts == 1
    assert repository.get_run(run.id).started_at is not None
    assert repository.get_run(run.id).finished_at is not None
    step = repository.list_steps(run.id)[0]
    attempt = repository.list_attempts(step.id)[0]
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.output.data == {"value": 7}
    assert attempt.input_hash is not None
    assert {event.event_type for event in repository.list_events(run.id)} >= {
        "run.created",
        "run.transition.executing",
        "step.attempt.succeeded",
        "run.transition.succeeded",
    }


def test_dependency_order_and_safe_output_passing(service_factory):
    _, registry, service = service_factory
    calls = []

    def source(data, context):
        calls.append("source")
        return ValueOutput(value={"items": ["cleaned", "row2"]})

    def sink(data, context):
        calls.append("sink")
        return ValueOutput(value=data.value)

    register_tool(registry, "source", source)
    register_tool(registry, "sink", sink)
    workflow = plan(
        PlanStep(
            step_key="consume",
            tool_name="sink",
            purpose="Use source output",
            depends_on=["produce"],
            arguments={
                "value": {"$agentflow_ref": {"step_key": "produce", "pointer": "/value/items/0"}}
            },
        ),
        PlanStep(step_key="produce", tool_name="source", purpose="Produce data"),
    )
    run = service.create_run(workflow)

    result = service.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    assert calls == ["source", "sink"]
    assert result.steps[0].output == {"value": "cleaned"}


def test_invalid_output_reference_fails_and_is_persisted(service_factory):
    repository, registry, service = service_factory
    register_tool(registry, "source", lambda data, context: ValueOutput(value={"ok": 1}))
    register_tool(registry, "sink", lambda data, context: ValueOutput(value=data.value))
    run = service.create_run(
        plan(
            PlanStep(step_key="source", tool_name="source", purpose="Produce"),
            PlanStep(
                step_key="sink",
                tool_name="sink",
                purpose="Read missing path",
                depends_on=["source"],
                arguments={
                    "value": {"$agentflow_ref": {"step_key": "source", "pointer": "/missing"}}
                },
            ),
        )
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.FAILED
    assert result.steps[1].status == StepStatus.FAILED
    sink_step = repository.list_steps(run.id)[1]
    assert repository.list_attempts(sink_step.id)[0].error_code == "invalid_output_reference"


def test_required_failure_skips_dependants_but_runs_independent_steps(service_factory):
    repository, registry, service = service_factory
    calls = []

    def fail(data, context):
        calls.append("fail")
        raise ToolPermissionError("private path must not be persisted")

    def run(data, context):
        calls.append(str(data.value))
        return ValueOutput(value=data.value)

    register_tool(registry, "failing", fail)
    register_tool(registry, "echo", run)
    workflow = plan(
        PlanStep(step_key="bad", tool_name="failing", purpose="Fail"),
        PlanStep(
            step_key="child",
            tool_name="echo",
            purpose="Depends on failure",
            depends_on=["bad"],
            arguments={"value": "child"},
        ),
        PlanStep(
            step_key="independent",
            tool_name="echo",
            purpose="Independent work",
            arguments={"value": "independent"},
        ),
    )
    run_record = service.create_run(workflow)

    result = service.execute(run_record.id)

    assert result.status == RunStatus.FAILED
    assert calls == ["fail", "independent"]
    assert [step.status for step in result.steps] == [
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.SUCCEEDED,
    ]
    assert "private path" not in (repository.get_run(run_record.id).error_summary or "")


def test_optional_failure_is_retained_without_failing_workflow(service_factory):
    repository, registry, service = service_factory

    def fail(data, context):
        raise ToolInputError("invalid test input", tool_name="optional_tool")

    register_tool(registry, "optional_tool", fail)
    run = service.create_run(
        plan(
            PlanStep(
                step_key="optional",
                tool_name="optional_tool",
                purpose="Optional analysis",
                required=False,
            )
        )
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    assert result.steps[0].status == StepStatus.FAILED
    assert result.steps[0].failure.code == "invalid_input"
    assert (
        repository.list_attempts(repository.list_steps(run.id)[0].id)[0].status
        == AttemptStatus.FAILED
    )
    assert any(event.event_type == "run.optional_steps_failed" for event in result.events)


def test_retry_requires_transient_error_and_safe_tool_metadata(service_factory):
    repository, registry, service = service_factory
    calls = 0

    def transient(data, context):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ToolTransientError(
                "remote service busy", tool_name="retry_tool", tool_version="1.0.0"
            )
        return ValueOutput(value="done")

    register_tool(registry, "retry_tool", transient, retryable=True, idempotent=True)
    run = service.create_run(
        plan(PlanStep(step_key="retry", tool_name="retry_tool", purpose="Retry safely"))
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    assert result.steps[0].attempts == 3
    step_id = repository.list_steps(run.id)[0].id
    attempts = repository.list_attempts(step_id)
    assert [item.status for item in attempts] == [
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    ]
    assert [item.attempt_number for item in attempts] == [1, 2, 3]


def test_retry_limit_exhaustion_persists_each_attempt(service_factory):
    repository, registry, service = service_factory
    calls = 0

    def always_transient(data, context):
        nonlocal calls
        calls += 1
        raise ToolTransientError(
            "temporary failure", tool_name="always_transient", tool_version="1.0.0"
        )

    register_tool(registry, "always_transient", always_transient, retryable=True)
    run = service.create_run(
        plan(
            PlanStep(
                step_key="attempted",
                tool_name="always_transient",
                purpose="Exhaust the bounded retry policy",
            )
        )
    )

    result = service.execute(run.id)

    attempts = repository.list_attempts(repository.list_steps(run.id)[0].id)
    assert result.status == RunStatus.FAILED
    assert calls == 3
    assert len(attempts) == 3
    assert all(attempt.status == AttemptStatus.FAILED for attempt in attempts)


def test_timeout_is_uncertain_and_never_retried(service_factory, monkeypatch):
    _, registry, service = service_factory
    register_tool(
        registry, "side_effect", lambda data, context: ValueOutput(value=1), retryable=True
    )
    calls = 0

    def timeout(name, inputs, *, context=None, version=None):
        nonlocal calls
        calls += 1
        raise ToolTimeoutError(name, version or "1.0.0", 0.01)

    monkeypatch.setattr(registry, "invoke", timeout)
    run = service.create_run(
        plan(PlanStep(step_key="effect", tool_name="side_effect", purpose="May have acted"))
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.PAUSED
    assert result.failure.code == "timeout"
    assert calls == 1


def test_user_cancel_during_uncertain_tool_is_distinguished(service_factory, monkeypatch):
    repository, registry, service = service_factory
    register_tool(registry, "side_effect", lambda data, context: ValueOutput(value=1))
    run = service.create_run(
        plan(PlanStep(step_key="effect", tool_name="side_effect", purpose="May have acted"))
    )

    def timeout_after_cancel(name, inputs, *, context=None, version=None):
        assert service.cancel(run.id) is True
        raise ToolTimeoutError(name, version or "1.0.0", 0.01)

    monkeypatch.setattr(registry, "invoke", timeout_after_cancel)

    result = service.execute(run.id)

    persisted = repository.get_run(run.id)
    assert result.status == RunStatus.PAUSED
    assert persisted.error_code == "user_cancel_outcome_uncertain"
    assert any(event.event_type == "run.cancel_uncertain" for event in result.events)


@pytest.mark.parametrize(
    "failure",
    [
        ToolInputError("bad input", tool_name="locked"),
        ToolPermissionError("denied", tool_name="locked"),
    ],
)
def test_validation_and_permission_errors_are_never_retried(service_factory, failure):
    repository, registry, service = service_factory
    calls = 0

    def always_fail(data, context):
        nonlocal calls
        calls += 1
        raise failure

    register_tool(registry, "locked", always_fail, retryable=True, idempotent=True)
    run = service.create_run(plan(PlanStep(step_key="locked", tool_name="locked", purpose="Fail")))

    result = service.execute(run.id)

    assert result.status == RunStatus.FAILED
    assert calls == 1
    assert result.steps[0].attempts == 1
    assert len(repository.list_attempts(repository.list_steps(run.id)[0].id)) == 1


def test_non_idempotent_tool_is_not_retryable(service_factory):
    repository, registry, service = service_factory
    calls = 0

    def always_transient(data, context):
        nonlocal calls
        calls += 1
        raise ToolTransientError("safe transient", tool_name="transient", tool_version="1.0.0")

    register_tool(registry, "transient", always_transient, retryable=False, idempotent=False)
    run = service.create_run(
        plan(PlanStep(step_key="transient", tool_name="transient", purpose="Bounded retry"))
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.FAILED
    assert calls == 1
    assert result.steps[0].attempts == 1
    assert len(repository.list_attempts(repository.list_steps(run.id)[0].id)) == 1


def test_run_pins_tool_version_at_creation(service_factory):
    repository, registry, service = service_factory
    calls = []
    register_tool(
        registry,
        "versioned",
        lambda data, context: calls.append("1.0.0") or ValueOutput(value="old"),
        version="1.0.0",
    )
    register_tool(
        registry,
        "versioned",
        lambda data, context: calls.append("2.0.0") or ValueOutput(value="pinned"),
        version="2.0.0",
    )
    run = service.create_run(
        plan(PlanStep(step_key="versioned", tool_name="versioned", purpose="Pinned"))
    )
    assert repository.list_steps(run.id)[0].tool_version == "2.0.0"
    register_tool(
        registry,
        "versioned",
        lambda data, context: calls.append("3.0.0") or ValueOutput(value="new"),
        version="3.0.0",
    )

    result = service.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    assert calls == ["2.0.0"]


def test_cancel_before_execution_persists_terminal_states_and_never_calls_tool(service_factory):
    repository, registry, service = service_factory
    calls = []
    register_tool(registry, "never", lambda data, context: calls.append(1) or ValueOutput(value=1))
    run = service.create_run(
        plan(PlanStep(step_key="never", tool_name="never", purpose="Do not run"))
    )

    assert service.cancel(run.id) is True
    result = service.execute(run.id)

    assert result.status == RunStatus.CANCELLED
    assert repository.list_steps(run.id)[0].status == StepStatus.CANCELLED
    assert calls == []
    assert any(event.event_type == "run.cancelled" for event in result.events)


def test_cancel_between_steps_prevents_next_invocation(service_factory):
    repository, registry, service = service_factory
    calls = []
    run_holder = {}

    def cancel_after_first(data, context):
        calls.append("first")
        service.cancel(run_holder["id"])
        return ValueOutput(value="finished")

    def second(data, context):
        calls.append("second")
        return ValueOutput(value="should not run")

    register_tool(registry, "first_tool", cancel_after_first)
    register_tool(registry, "second_tool", second)
    run = service.create_run(
        plan(
            PlanStep(step_key="first", tool_name="first_tool", purpose="Request cancellation"),
            PlanStep(step_key="second", tool_name="second_tool", purpose="Must not start"),
        )
    )
    run_holder["id"] = run.id

    result = service.execute(run.id)

    assert result.status == RunStatus.CANCELLED
    assert calls == ["first"]
    assert result.steps[0].status == StepStatus.SUCCEEDED
    assert result.steps[1].status == StepStatus.CANCELLED
    assert repository.get_run(run.id).error_code == "user_cancelled"


def test_step_approval_requires_exact_persisted_run_step_digest_and_status(service_factory):
    repository, registry, service = service_factory
    calls = []
    register_tool(
        registry,
        "protected",
        lambda data, context: calls.append(context.approved) or ValueOutput(value="approved"),
        requires_approval=True,
    )
    run = service.create_run(
        plan(PlanStep(step_key="protected", tool_name="protected", purpose="Protected call"))
    )
    first = service.execute(run.id)
    assert first.status == RunStatus.AWAITING_APPROVAL
    request = repository.list_approvals(run.id)[0]
    assert request.run_id == run.id
    assert request.step_id == repository.list_steps(run.id)[0].id
    assert calls == []

    wrong_scope = ApprovalRequest.model_validate(
        {
            **request.model_dump(),
            "step_id": None,
            "status": ApprovalStatus.APPROVED,
            "resolved_at": datetime.now(UTC),
        }
    )
    repository.update_approval(wrong_scope)
    still_waiting = service.execute(run.id)
    assert still_waiting.status == RunStatus.AWAITING_APPROVAL
    assert calls == []

    exact_request = next(
        item for item in repository.list_approvals(run.id) if item.step_id == request.step_id
    )
    unresolved_approval = ApprovalRequest.model_validate(
        {**exact_request.model_dump(), "status": ApprovalStatus.APPROVED, "resolved_at": None}
    )
    repository.update_approval(unresolved_approval)
    still_waiting = service.execute(run.id)
    assert still_waiting.status == RunStatus.AWAITING_APPROVAL
    assert calls == []

    resolved = ApprovalRequest.model_validate(
        {
            **unresolved_approval.model_dump(),
            "resolved_at": datetime.now(UTC),
        }
    )
    repository.update_approval(resolved)
    completed = service.execute(run.id)
    assert completed.status == RunStatus.SUCCEEDED
    assert calls == [True]

    with pytest.raises(TypeError):
        service.execute(run.id, approved=True)


def test_plan_approval_is_persisted_and_verified(service_factory):
    repository, registry, service = service_factory
    calls = []
    register_tool(
        registry, "plan_tool", lambda data, context: calls.append(1) or ValueOutput(value=1)
    )
    run = service.create_run(
        plan(
            PlanStep(step_key="step", tool_name="plan_tool", purpose="Approved plan"),
            requires_approval=True,
        )
    )
    waiting = service.execute(run.id)
    assert waiting.status == RunStatus.AWAITING_APPROVAL
    approval = repository.list_approvals(run.id)[0]
    assert approval.step_id is None
    repository.update_approval(
        ApprovalRequest.model_validate(
            {
                **approval.model_dump(),
                "status": ApprovalStatus.APPROVED,
                "resolved_at": datetime.now(UTC),
            }
        )
    )
    assert service.execute(run.id).status == RunStatus.SUCCEEDED
    assert calls == [1]


def test_interrupted_attempt_is_recorded_paused_and_never_replayed(service_factory):
    repository, registry, service = service_factory
    calls = []
    register_tool(registry, "unsafe", lambda data, context: calls.append(1) or ValueOutput(value=1))
    run = service.create_run(
        plan(PlanStep(step_key="unsafe", tool_name="unsafe", purpose="May be uncertain"))
    )
    step = repository.list_steps(run.id)[0]
    with repository.transaction():
        repository.transition_run(run.id, RunStatus.PLANNING)
        repository.transition_run(run.id, RunStatus.EXECUTING)
        repository.transition_step(step.id, StepStatus.READY)
        repository.transition_step(step.id, StepStatus.RUNNING)
        repository.create_attempt(StepAttempt(step_id=step.id, attempt_number=1))

    recovered = service.recover_interrupted_run(run.id)

    assert recovered.status == RunStatus.PAUSED
    assert repository.get_run(run.id).error_code == "interrupted"
    assert any(event.event_type == "run.interrupted" for event in recovered.events)
    attempt = repository.list_attempts(step.id)[0]
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.error_code == "interrupted_uncertain"
    with pytest.raises(RunNotExecutableError):
        service.execute(run.id)
    assert calls == []


def test_outputs_are_isolated_between_run_ids(service_factory):
    _, registry, service = service_factory
    counter = 0
    received = []

    def source(data, context):
        nonlocal counter
        counter += 1
        return ValueOutput(value=counter)

    def sink(data, context):
        received.append(data.value)
        return ValueOutput(value=data.value)

    register_tool(registry, "source", source)
    register_tool(registry, "sink", sink)
    workflow = plan(
        PlanStep(step_key="source", tool_name="source", purpose="Source"),
        PlanStep(
            step_key="sink",
            tool_name="sink",
            purpose="Sink",
            depends_on=["source"],
            arguments={"value": {"$agentflow_ref": {"step_key": "source", "pointer": "/value"}}},
        ),
    )
    first = service.create_run(workflow)
    second = service.create_run(workflow)

    assert service.execute(first.id).status == RunStatus.SUCCEEDED
    assert service.execute(second.id).status == RunStatus.SUCCEEDED
    assert received == [1, 2]


def test_recovery_rejects_in_process_run(service_factory):
    repository, registry, service = service_factory
    started = threading.Event()
    release = threading.Event()
    register_tool(
        registry,
        "waiter",
        lambda data, context: (started.set(), release.wait(), ValueOutput(value=1))[2],
    )
    run = service.create_run(plan(PlanStep(step_key="wait", tool_name="waiter", purpose="Block")))
    thread = threading.Thread(target=lambda: service.execute(run.id))
    thread.start()
    try:
        assert started.wait(2)
        with pytest.raises(RunNotExecutableError):
            service.recover_interrupted_run(run.id)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
