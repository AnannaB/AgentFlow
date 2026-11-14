"""Lifecycle transition policy coverage."""

import pytest

from agentflow.domain.errors import InvalidStateTransition
from agentflow.domain.states import RunStatus, StepStatus
from agentflow.domain.transitions import ensure_run_transition, ensure_step_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.QUEUED, RunStatus.PLANNING),
        (RunStatus.PLANNING, RunStatus.EXECUTING),
        (RunStatus.EXECUTING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.SUCCEEDED),
        (RunStatus.PAUSED, RunStatus.EXECUTING),
    ],
)
def test_allowed_run_transitions(current: RunStatus, target: RunStatus) -> None:
    ensure_run_transition(current, target)


@pytest.mark.parametrize("terminal", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED])
def test_terminal_run_statuses_reject_all_transitions(terminal: RunStatus) -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_run_transition(terminal, RunStatus.EXECUTING)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (StepStatus.PENDING, StepStatus.READY),
        (StepStatus.READY, StepStatus.RUNNING),
        (StepStatus.RUNNING, StepStatus.SUCCEEDED),
        (StepStatus.FAILED, StepStatus.READY),
        (StepStatus.BLOCKED, StepStatus.PENDING),
    ],
)
def test_allowed_step_transitions(current: StepStatus, target: StepStatus) -> None:
    ensure_step_transition(current, target)


@pytest.mark.parametrize(
    "terminal", [StepStatus.SUCCEEDED, StepStatus.SKIPPED, StepStatus.CANCELLED]
)
def test_terminal_step_statuses_reject_all_transitions(terminal: StepStatus) -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_step_transition(terminal, StepStatus.READY)


def test_invalid_transition_reports_entity_and_states() -> None:
    with pytest.raises(InvalidStateTransition, match="queued -> succeeded"):
        ensure_run_transition(RunStatus.QUEUED, RunStatus.SUCCEEDED)


@pytest.mark.parametrize(
    ("transition", "current", "target"),
    [
        (ensure_run_transition, RunStatus.QUEUED, "planning"),
        (ensure_step_transition, StepStatus.PENDING, "ready"),
    ],
)
def test_transition_rejects_raw_string_state_values(transition, current, target) -> None:
    with pytest.raises(TypeError, match="require"):
        transition(current, target)
