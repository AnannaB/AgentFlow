"""Centralized lifecycle transition rules."""

from agentflow.domain.errors import InvalidStateTransition
from agentflow.domain.states import RunStatus, StepStatus

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PLANNING, RunStatus.CANCELLED}),
    RunStatus.PLANNING: frozenset(
        {
            RunStatus.PLAN_REVIEW,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.EXECUTING,
            RunStatus.PAUSED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PLAN_REVIEW: frozenset(
        {RunStatus.AWAITING_APPROVAL, RunStatus.EXECUTING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {RunStatus.EXECUTING, RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.EXECUTING: frozenset(
        {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.VALIDATING,
            RunStatus.PAUSED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.VALIDATING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.PAUSED: frozenset(
        {RunStatus.PLANNING, RunStatus.EXECUTING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset(
        {StepStatus.READY, StepStatus.SKIPPED, StepStatus.BLOCKED, StepStatus.CANCELLED}
    ),
    StepStatus.READY: frozenset(
        {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.BLOCKED, StepStatus.CANCELLED}
    ),
    StepStatus.RUNNING: frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}),
    StepStatus.FAILED: frozenset({StepStatus.READY, StepStatus.CANCELLED}),
    StepStatus.BLOCKED: frozenset({StepStatus.PENDING, StepStatus.READY, StepStatus.CANCELLED}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Raise if a run transition is not allowed by the lifecycle policy."""
    if not isinstance(current, RunStatus) or not isinstance(target, RunStatus):
        raise TypeError("run transitions require RunStatus values")
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidStateTransition("run", current.value, target.value)


def ensure_step_transition(current: StepStatus, target: StepStatus) -> None:
    """Raise if a step transition is not allowed by the lifecycle policy."""
    if not isinstance(current, StepStatus) or not isinstance(target, StepStatus):
        raise TypeError("step transitions require StepStatus values")
    if target not in STEP_TRANSITIONS[current]:
        raise InvalidStateTransition("step", current.value, target.value)
