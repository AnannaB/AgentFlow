"""Integration coverage for migrations, repositories, and transaction behavior."""

from datetime import UTC, datetime
from sqlite3 import IntegrityError

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
from agentflow.domain.states import ApprovalStatus, AttemptStatus, RunStatus, StepStatus
from agentflow.storage.sqlite import PersistenceError, SQLiteDatabase, SQLiteRepository


@pytest.fixture
def database(tmp_path):
    db = SQLiteDatabase(tmp_path / "agentflow.sqlite3")
    db.initialize()
    return db


@pytest.fixture
def repository(database):
    return SQLiteRepository(database)


def make_plan() -> ExecutionPlan:
    return ExecutionPlan(
        objective_summary="Summarize a local report",
        steps=[PlanStep(step_key="read", tool_name="read_report", purpose="Read report")],
        completion_criteria=["A summary is available"],
    )


def test_database_initializes_idempotently_with_explicit_migration(database) -> None:
    database.initialize()
    with database.connection() as connection:
        versions = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert [(row["version"], row["name"]) for row in versions] == [
        (1, "initial"),
        (2, "workflow_step_tool_version"),
    ]
    assert {
        "workflow_runs",
        "workflow_steps",
        "step_attempts",
        "approval_requests",
        "run_events",
        "workflow_templates",
    } <= tables


def test_crud_for_every_persisted_entity(repository) -> None:
    run = repository.create_run(WorkflowRun(objective="Analyze report", plan=make_plan()))
    assert repository.get_run(run.id) == run
    assert repository.list_runs() == [run]

    updated_run = WorkflowRun.model_validate({**run.model_dump(), "objective": "Updated objective"})
    assert repository.update_run(updated_run) == updated_run
    assert repository.get_run(run.id) == updated_run

    step = repository.create_step(
        WorkflowStep(run_id=run.id, step_key="read", position=0, tool_name="read_report")
    )
    assert repository.get_step(step.id) == step
    assert repository.list_steps(run.id) == [step]
    updated_step = WorkflowStep.model_validate(
        {**step.model_dump(), "arguments": {"path": "report.csv"}}
    )
    assert repository.update_step(updated_step) == updated_step

    attempt = repository.create_attempt(
        StepAttempt(
            step_id=step.id, attempt_number=1, output=ToolResult(success=True, summary="Read")
        )
    )
    assert repository.get_attempt(attempt.id) == attempt
    assert repository.list_attempts(step.id) == [attempt]
    updated_attempt = StepAttempt.model_validate(
        {**attempt.model_dump(), "status": AttemptStatus.SUCCEEDED, "duration_ms": 25}
    )
    assert repository.update_attempt(updated_attempt) == updated_attempt
    assert repository.get_attempt(attempt.id) == updated_attempt

    approval = repository.create_approval(ApprovalRequest(run_id=run.id, plan_digest="digest-v1"))
    assert repository.get_approval(approval.id) == approval
    assert repository.list_approvals(run.id) == [approval]
    resolved = ApprovalRequest.model_validate(
        {
            **approval.model_dump(),
            "status": ApprovalStatus.APPROVED,
            "decision_note": "Reviewed",
            "resolved_at": datetime.now(UTC),
        }
    )
    repository.update_approval(resolved)
    assert repository.get_approval(approval.id) == resolved

    event = repository.create_event(RunEvent(run_id=run.id, event_type="run.created"))
    assert repository.get_event(event.id) == event
    assert repository.list_events(run.id) == [event]
    changed_event = RunEvent.model_validate({**event.model_dump(), "event_type": "run.created.v2"})
    repository.update_event(changed_event)
    assert repository.get_event(event.id) == changed_event

    template = repository.create_template(WorkflowTemplate(name="Report summary", plan=make_plan()))
    assert repository.get_template(template.id) == template
    assert repository.list_templates() == [template]
    revised_template = WorkflowTemplate.model_validate(
        {**template.model_dump(), "name": "Monthly report", "version": 2}
    )
    repository.update_template(revised_template)
    assert repository.get_template(template.id) == revised_template

    assert repository.delete_event(event.id)
    assert repository.get_event(event.id) is None
    assert repository.delete_approval(approval.id)
    assert repository.get_approval(approval.id) is None
    assert repository.delete_attempt(attempt.id)
    assert repository.get_attempt(attempt.id) is None
    assert repository.delete_step(step.id)
    assert repository.get_step(step.id) is None
    assert repository.delete_template(template.id)
    assert repository.get_template(template.id) is None
    assert repository.delete_run(run.id)
    assert repository.get_run(run.id) is None


def test_approval_and_template_persist_after_database_reopen(database, tmp_path) -> None:
    repository = SQLiteRepository(database)
    run = repository.create_run(WorkflowRun(objective="Review objective"))
    approval = repository.create_approval(
        ApprovalRequest(run_id=run.id, plan_digest="sha256-value")
    )
    template = repository.create_template(WorkflowTemplate(name="Safe template", plan=make_plan()))

    reopened = SQLiteDatabase(tmp_path / "agentflow.sqlite3")
    reopened.initialize()
    repository_after_reopen = SQLiteRepository(reopened)

    assert repository_after_reopen.get_approval(approval.id) == approval
    assert repository_after_reopen.get_template(template.id) == template


def test_step_approval_must_belong_to_its_run(repository) -> None:
    first_run = repository.create_run(WorkflowRun(objective="First run"))
    second_run = repository.create_run(WorkflowRun(objective="Second run"))
    step = repository.create_step(
        WorkflowStep(run_id=first_run.id, step_key="first", position=0, tool_name="tool")
    )

    with pytest.raises(ValueError, match="same workflow run"):
        repository.create_approval(
            ApprovalRequest(run_id=second_run.id, step_id=step.id, plan_digest="digest")
        )

    with repository.database.transaction() as connection:
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                """INSERT INTO approval_requests (
                    id, run_id, step_id, status, plan_digest, requested_at
                ) VALUES (?, ?, ?, 'pending', 'digest', ?)""",
                (
                    "direct-mismatch",
                    str(second_run.id),
                    str(step.id),
                    datetime.now(UTC).isoformat(),
                ),
            )

    approval = repository.create_approval(
        ApprovalRequest(run_id=first_run.id, step_id=step.id, plan_digest="matching-digest")
    )
    assert repository.delete_step(step.id)
    assert repository.get_approval(approval.id) is None


def test_foreign_keys_are_enforced_and_cascades_are_consistent(repository, database) -> None:
    missing_id = WorkflowRun(objective="Missing").id
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        repository.create_step(
            WorkflowStep(run_id=missing_id, step_key="orphan", position=0, tool_name="tool")
        )

    run = repository.create_run(WorkflowRun(objective="Has step"))
    step = repository.create_step(
        WorkflowStep(run_id=run.id, step_key="child", position=0, tool_name="tool")
    )
    attempt = repository.create_attempt(StepAttempt(step_id=step.id, attempt_number=1))
    assert repository.delete_run(run.id)
    assert repository.get_step(step.id) is None
    assert repository.get_attempt(attempt.id) is None

    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_transaction_rolls_back_related_repository_operations(repository) -> None:
    run = WorkflowRun(objective="Should roll back")
    step = WorkflowStep(run_id=run.id, step_key="step", position=0, tool_name="tool")

    with pytest.raises(RuntimeError, match="force rollback"):
        with repository.transaction():
            repository.create_run(run)
            repository.create_step(step)
            raise RuntimeError("force rollback")

    assert repository.get_run(run.id) is None
    assert repository.get_step(step.id) is None


def test_run_transition_and_event_are_atomic(repository) -> None:
    run = repository.create_run(WorkflowRun(objective="Transition me"))
    planning = repository.transition_run(run.id, RunStatus.PLANNING)
    assert planning.status == RunStatus.PLANNING
    assert [event.event_type for event in repository.list_events(run.id)] == [
        "run.transition.planning"
    ]

    with pytest.raises(ValidationError):
        repository.transition_run(run.id, RunStatus.EXECUTING, details={"not_json": object()})
    assert repository.get_run(run.id).status == RunStatus.PLANNING
    assert len(repository.list_events(run.id)) == 1


def test_step_transitions_are_validated_and_recorded(repository) -> None:
    run = repository.create_run(WorkflowRun(objective="Step transitions"))
    step = repository.create_step(
        WorkflowStep(run_id=run.id, step_key="one", position=0, tool_name="tool")
    )

    ready = repository.transition_step(step.id, StepStatus.READY)
    running = repository.transition_step(step.id, StepStatus.RUNNING)
    succeeded = repository.transition_step(step.id, StepStatus.SUCCEEDED)

    assert ready.status == StepStatus.READY
    assert running.started_at is not None
    assert succeeded.finished_at is not None
    with pytest.raises(ValueError, match="Invalid step transition"):
        repository.transition_step(step.id, StepStatus.READY)
    assert len(repository.list_events(run.id)) == 3


def test_state_changes_cannot_bypass_transition_api(repository) -> None:
    run = repository.create_run(WorkflowRun(objective="State guarded"))
    changed = WorkflowRun.model_validate({**run.model_dump(), "status": RunStatus.SUCCEEDED})

    with pytest.raises(ValueError, match="Invalid run transition"):
        repository.update_run(changed)
    assert repository.get_run(run.id).status == RunStatus.QUEUED

    step = repository.create_step(
        WorkflowStep(run_id=run.id, step_key="guarded", position=0, tool_name="tool")
    )
    changed_step = WorkflowStep.model_validate(
        {**step.model_dump(), "status": StepStatus.SUCCEEDED}
    )
    with pytest.raises(ValueError, match="Invalid step transition"):
        repository.update_step(changed_step)
    assert repository.get_step(step.id).status == StepStatus.PENDING


def test_terminal_run_and_step_records_cannot_be_mutated(repository) -> None:
    run = repository.create_run(WorkflowRun(objective="Terminal run"))
    repository.transition_run(run.id, RunStatus.PLANNING)
    repository.transition_run(run.id, RunStatus.EXECUTING)
    repository.transition_run(run.id, RunStatus.VALIDATING)
    completed = repository.transition_run(run.id, RunStatus.SUCCEEDED)
    modified_run = WorkflowRun.model_validate(
        {**completed.model_dump(), "objective": "Changed after completion"}
    )
    with pytest.raises(PersistenceError, match="terminal workflow run"):
        repository.update_run(modified_run)

    step = repository.create_step(
        WorkflowStep(run_id=run.id, step_key="done", position=0, tool_name="tool")
    )
    repository.transition_step(step.id, StepStatus.READY)
    repository.transition_step(step.id, StepStatus.RUNNING)
    completed_step = repository.transition_step(step.id, StepStatus.SUCCEEDED)
    modified_step = WorkflowStep.model_validate(
        {**completed_step.model_dump(), "tool_name": "different_tool"}
    )
    with pytest.raises(PersistenceError, match="terminal workflow step"):
        repository.update_step(modified_step)


def test_bounded_history_limit_and_stable_ordering(repository) -> None:
    for objective in ("first", "second", "third"):
        repository.create_run(WorkflowRun(objective=objective))

    page = repository.list_runs(limit=2)
    assert len(page) == 2
    with pytest.raises(ValueError, match="limit"):
        repository.list_runs(limit=201)
    with pytest.raises(ValueError, match="limit"):
        repository.list_events(WorkflowRun(objective="temporary").id, limit=0)


def test_run_history_cursor_pages_without_duplicates(repository) -> None:
    for index in range(5):
        created = datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC)
        repository.create_run(
            WorkflowRun(objective=f"run {index}", created_at=created, updated_at=created)
        )

    first_page = repository.list_runs(limit=2)
    second_page = repository.list_runs(
        limit=2, before=(first_page[-1].created_at, first_page[-1].id)
    )

    assert [run.objective for run in first_page] == ["run 4", "run 3"]
    assert [run.objective for run in second_page] == ["run 2", "run 1"]
    assert not {run.id for run in first_page} & {run.id for run in second_page}
    with pytest.raises(ValueError, match="UTC"):
        repository.list_runs(limit=2, before=(datetime(2026, 1, 1), first_page[-1].id))


def test_invalid_models_and_duplicate_template_names_are_rejected(repository) -> None:
    with pytest.raises(ValidationError):
        WorkflowRun(objective="")
    repository.create_template(WorkflowTemplate(name="Unique name", plan=make_plan()))
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        repository.create_template(WorkflowTemplate(name="unique NAME", plan=make_plan()))


def test_repository_revalidates_mutable_nested_payloads_before_persist(repository) -> None:
    run = WorkflowRun(objective="Mutated after validation", plan=make_plan())
    run.plan.steps.append(
        PlanStep(step_key="read", tool_name="unexpected", purpose="Duplicate key")
    )
    with pytest.raises(ValidationError, match="step_key values must be unique"):
        repository.create_run(run)
    assert repository.get_run(run.id) is None

    event = RunEvent(run_id=WorkflowRun(objective="event owner").id, event_type="unsafe")
    event.details["password"] = "added after model validation"
    with pytest.raises(ValidationError, match="sensitive credential fields"):
        repository.create_event(event)
    assert repository.get_event(event.id) is None
