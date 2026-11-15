"""Sequential, persistent execution of validated workflows using the tool registry."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from agentflow.application.errors import (
    InvalidOutputReferenceError,
    InvalidWorkflowPlanError,
    RunNotExecutableError,
    RunNotFoundError,
)
from agentflow.application.models import ExecutionResult, FailureDetail, StepExecutionResult
from agentflow.application.references import resolve_step_arguments
from agentflow.domain.models import (
    ApprovalRequest,
    DomainModel,
    ExecutionPlan,
    RunEvent,
    StepAttempt,
    ToolResult,
    WorkflowRun,
    WorkflowStep,
    utc_now,
)
from agentflow.domain.states import (
    ApprovalStatus,
    AttemptStatus,
    PlannerKind,
    RunStatus,
    StepStatus,
)
from agentflow.storage.protocols import WorkflowRepository
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import (
    ToolApprovalRequired,
    ToolBusyError,
    ToolError,
    ToolInputError,
    ToolOutputError,
    ToolPermissionError,
    ToolTimeoutError,
    ToolTransientError,
)
from agentflow.tools.registry import ToolRegistry


class RetryPolicy(DomainModel):
    """Bounded retries and interruptible exponential backoff configuration."""

    max_retries: int = Field(default=2, ge=0, le=3)
    initial_backoff_seconds: float = Field(default=0.1, ge=0, le=5)
    max_backoff_seconds: float = Field(default=2, ge=0, le=10)

    @model_validator(mode="after")
    def maximum_delay_is_not_below_initial(self) -> RetryPolicy:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff must be at least the initial backoff")
        return self

    def delay_for_retry(self, retry_number: int) -> float:
        """Return a capped exponential delay for the 1-based retry number."""
        if retry_number < 1:
            raise ValueError("retry_number must be at least one")
        return min(
            self.initial_backoff_seconds * (2 ** (retry_number - 1)),
            self.max_backoff_seconds,
        )


ApprovalDecision = Literal["approved", "pending", "rejected"]


class WorkflowExecutionService:
    """Create, execute, cancel, and safely recover persistent workflow runs."""

    def __init__(
        self,
        repository: WorkflowRepository,
        registry: ToolRegistry,
        *,
        retry_policy: RetryPolicy | None = None,
        wait: Callable[[float, threading.Event], bool] | None = None,
    ) -> WorkflowStep:
        self._repository = repository
        self._registry = registry
        self._retry_policy = retry_policy or RetryPolicy()
        self._wait = wait or _interruptible_wait
        self._active_contexts: dict[UUID, ToolExecutionContext] = {}
        self._active_lock = threading.RLock()

    def create_run(
        self,
        plan: ExecutionPlan,
        *,
        objective: str | None = None,
        planner_kind: PlannerKind = PlannerKind.RULE_BASED,
    ) -> WorkflowRun:
        """Persist a plan snapshot and ordered steps without executing any tool."""
        snapshot = _validated_plan_snapshot(plan)
        definitions = [self._registry.get(step.tool_name) for step in snapshot.steps]
        run = WorkflowRun(
            objective=objective if objective is not None else snapshot.objective_summary,
            plan=snapshot,
            planner_kind=planner_kind,
        )
        steps = [
            WorkflowStep(
                run_id=run.id,
                step_key=step.step_key,
                position=position,
                tool_name=step.tool_name,
                tool_version=definitions[position].metadata.version,
                arguments=step.arguments,
                depends_on=step.depends_on,
                required=step.required,
                approval_required=(
                    step.risk_level != "low" or definitions[position].metadata.requires_approval
                ),
            )
            for position, step in enumerate(snapshot.steps)
        ]
        with self._repository.transaction():
            self._repository.create_run(run)
            for step in steps:
                self._repository.create_step(step)
            self._repository.create_event(
                RunEvent(
                    run_id=run.id,
                    event_type="run.created",
                    details={"step_count": len(steps), "plan_digest": _digest_plan(snapshot)},
                )
            )
        return run

    def execute(
        self,
        run_id: UUID,
        *,
        workspace_roots: tuple[str, ...] = (),
    ) -> ExecutionResult:
        """Execute a persisted plan or continue a run awaiting persisted approval."""
        started = time.monotonic()
        root_context = ToolExecutionContext(workspace_roots=workspace_roots)
        run = self._get_run(run_id)
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return self._result(run_id, time.monotonic() - started)
        if run.status == RunStatus.PAUSED:
            reason = run.error_code or "paused"
            raise RunNotExecutableError(
                f"Paused run {run_id} requires explicit recovery ({reason})"
            )
        if run.status not in {RunStatus.QUEUED, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL}:
            raise RunNotExecutableError(f"Workflow run {run_id} is already executing")
        if run.plan is None:
            raise InvalidWorkflowPlanError("Persisted workflow run has no execution plan")
        plan = _validated_plan_snapshot(run.plan)
        steps = self._load_and_validate_steps(run, plan)
        with self._active_lock:
            if run_id in self._active_contexts:
                raise RunNotExecutableError(f"Workflow run {run_id} is already active")
            self._active_contexts[run_id] = root_context
        try:
            return self._execute_active(run, plan, steps, root_context, started)
        finally:
            with self._active_lock:
                self._active_contexts.pop(run_id, None)

    def cancel(self, run_id: UUID) -> bool:
        """Request cancellation; active synchronous tools receive cooperative cancellation."""
        with self._active_lock:
            active_context = self._active_contexts.get(run_id)
            if active_context is not None:
                active_context.cancel()
                return True

        run = self._get_run(run_id)
        if run.status in {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.SUCCEEDED}:
            return False
        if run.status == RunStatus.EXECUTING:
            raise RunNotExecutableError(
                "Run is executing in another process; use interrupted-run recovery instead"
            )
        with self._repository.transaction():
            current = self._get_run(run_id)
            if current.status in {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.SUCCEEDED}:
                return False
            if current.status not in {
                RunStatus.QUEUED,
                RunStatus.PLANNING,
                RunStatus.AWAITING_APPROVAL,
                RunStatus.PAUSED,
            }:
                raise RunNotExecutableError("Run cannot be cancelled from its current state")
            self._repository.update_run(
                WorkflowRun.model_validate(
                    {
                        **current.model_dump(),
                        "error_code": "user_cancelled",
                        "error_summary": "Execution was cancelled by the caller.",
                    }
                )
            )
            self._repository.transition_run(
                run_id,
                RunStatus.CANCELLED,
                details={"reason": "user_requested"},
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type="run.cancelled",
                    details={"reason": "user_requested"},
                )
            )
            for step in self._repository.list_steps(run_id):
                if step.status in {StepStatus.PENDING, StepStatus.READY}:
                    self._repository.transition_step(
                        step.id, StepStatus.CANCELLED, details={"reason": "user_requested"}
                    )
        return True

    def recover_interrupted_run(self, run_id: UUID) -> ExecutionResult:
        """Mark stale running attempts uncertain and paused; never replay them."""
        with self._active_lock:
            if run_id in self._active_contexts:
                raise RunNotExecutableError("An active in-process run cannot be recovered")
        run = self._get_run(run_id)
        if run.status != RunStatus.EXECUTING:
            raise RunNotExecutableError("Only a stale executing run can be recovered")

        with self._repository.transaction():
            current = self._get_run(run_id)
            if current.status != RunStatus.EXECUTING:
                raise RunNotExecutableError("Run state changed before recovery")
            for step in self._repository.list_steps(run_id):
                if step.status != StepStatus.RUNNING:
                    continue
                attempts = self._repository.list_attempts(step.id)
                running_attempt = next(
                    (
                        attempt
                        for attempt in reversed(attempts)
                        if attempt.status == AttemptStatus.RUNNING
                    ),
                    None,
                )
                now = utc_now()
                if running_attempt is not None:
                    self._repository.update_attempt(
                        StepAttempt.model_validate(
                            {
                                **running_attempt.model_dump(),
                                "status": AttemptStatus.FAILED,
                                "error_code": "interrupted_uncertain",
                                "error_summary": (
                                    "Process stopped during tool execution; outcome is uncertain "
                                    "and this attempt will not be replayed."
                                ),
                                "finished_at": now,
                                "duration_ms": max(
                                    0,
                                    int((now - running_attempt.started_at).total_seconds() * 1000),
                                ),
                            }
                        )
                    )
                self._repository.transition_step(
                    step.id,
                    StepStatus.FAILED,
                    details={"error_code": "interrupted_uncertain"},
                )
            self._repository.update_run(
                WorkflowRun.model_validate(
                    {
                        **current.model_dump(),
                        "error_code": "interrupted",
                        "error_summary": (
                            "Process interruption detected. Any running tool outcome is uncertain; "
                            "no attempt was replayed."
                        ),
                    }
                )
            )
            self._repository.transition_run(
                run_id,
                RunStatus.PAUSED,
                details={"reason": "process_interruption", "replay": "prohibited"},
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type="run.interrupted",
                    details={"error_code": "interrupted", "replay": "prohibited"},
                )
            )
        return self._result(run_id, 0)

    def _execute_active(
        self,
        run: WorkflowRun,
        plan: ExecutionPlan,
        steps: dict[str, WorkflowStep],
        context: ToolExecutionContext,
        started: float,
    ) -> ExecutionResult:
        run_id = run.id
        with self._repository.transaction():
            current = self._get_run(run_id)
            if current.status == RunStatus.QUEUED:
                current = self._repository.transition_run(run_id, RunStatus.PLANNING)
            if current.status not in {RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL}:
                raise RunNotExecutableError(f"Workflow run {run_id} cannot be claimed")

        if context_cancelled(context):
            self._cancel_remaining(run_id, steps)
            return self._finish_cancelled(run_id, {}, started)

        plan_digest = _digest_plan(plan)
        if plan.requires_approval:
            decision = self._approval_decision(run_id, None, plan_digest)
            if decision != "approved":
                if decision == "rejected":
                    return self._fail_run(
                        run_id,
                        FailureDetail(
                            code="approval_rejected", message="Plan approval was rejected."
                        ),
                        started,
                    )
                self._pause_for_approval(run_id, "plan", None, plan_digest)
                if context_cancelled(context):
                    self._cancel_remaining(run_id, steps)
                    return self._finish_cancelled(run_id, {}, started)
                return self._result(run_id, time.monotonic() - started)

        with self._repository.transaction():
            current = self._get_run(run_id)
            if current.status in {RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL}:
                self._repository.transition_run(run_id, RunStatus.EXECUTING)
            elif current.status != RunStatus.EXECUTING:
                raise RunNotExecutableError(f"Workflow run {run_id} cannot enter execution")

        successful_outputs = self._restore_successful_outputs(steps)
        failures = self._restore_failures(steps)
        uncertain: FailureDetail | None = None
        while True:
            if context_cancelled(context):
                self._cancel_remaining(run_id, steps)
                return self._finish_cancelled(run_id, failures, started)
            progress = False
            unfinished = False
            for plan_step in plan.steps:
                persisted = steps[plan_step.step_key]
                if persisted.status in {
                    StepStatus.SUCCEEDED,
                    StepStatus.FAILED,
                    StepStatus.SKIPPED,
                    StepStatus.CANCELLED,
                }:
                    continue
                unfinished = True
                dependency_states = [steps[key].status for key in plan_step.depends_on]
                if any(
                    state
                    in {
                        StepStatus.FAILED,
                        StepStatus.SKIPPED,
                        StepStatus.BLOCKED,
                        StepStatus.CANCELLED,
                    }
                    for state in dependency_states
                ):
                    steps[plan_step.step_key] = self._transition_step(
                        persisted,
                        StepStatus.SKIPPED,
                        {"reason": "dependency_did_not_succeed"},
                    )
                    progress = True
                    continue
                if not all(state == StepStatus.SUCCEEDED for state in dependency_states):
                    continue

                if persisted.tool_version is None:
                    raise InvalidWorkflowPlanError(
                        f"Step {plan_step.step_key!r} has no pinned tool version"
                    )
                definition = self._registry.get(plan_step.tool_name, persisted.tool_version)
                step_digest = _digest_step(plan_digest, plan_step, definition.metadata.version)
                step_requires_approval = persisted.approval_required
                step_approved = False
                if step_requires_approval:
                    decision = self._approval_decision(run_id, persisted.id, step_digest)
                    if decision == "rejected":
                        failures[plan_step.step_key] = FailureDetail(
                            code="approval_rejected",
                            message="Step approval was rejected.",
                            step_key=plan_step.step_key,
                        )
                        steps[plan_step.step_key] = self._transition_step(
                            persisted, StepStatus.SKIPPED, {"reason": "approval_rejected"}
                        )
                        progress = True
                        continue
                    if decision != "approved":
                        self._pause_for_approval(
                            run_id, plan_step.step_key, persisted.id, step_digest
                        )
                        if context_cancelled(context):
                            self._cancel_remaining(run_id, steps)
                            return self._finish_cancelled(run_id, failures, started)
                        return self._result(run_id, time.monotonic() - started)
                    step_approved = True

                if context_cancelled(context):
                    self._cancel_remaining(run_id, steps)
                    return self._finish_cancelled(run_id, failures, started)

                try:
                    updated, output, failure, is_uncertain = self._execute_step(
                        run_id,
                        plan_step.step_key,
                        plan_step.tool_name,
                        plan_step.depends_on,
                        persisted,
                        successful_outputs,
                        context.with_approval()
                        if (step_approved or plan.requires_approval)
                        else context,
                        definition.metadata.retryable and definition.metadata.idempotent,
                        persisted.tool_version,
                    )
                except InvalidOutputReferenceError:
                    updated, failure = self._record_reference_failure(
                        run_id, plan_step.step_key, plan_step.tool_name, persisted
                    )
                    output = None
                    is_uncertain = False
                steps[plan_step.step_key] = updated
                if output is not None:
                    successful_outputs[plan_step.step_key] = output
                if failure is not None:
                    failures[plan_step.step_key] = failure
                progress = True
                if is_uncertain:
                    uncertain = failure
                    break
                if context_cancelled(context):
                    self._cancel_remaining(run_id, steps)
                    return self._finish_cancelled(run_id, failures, started)
            if uncertain is not None:
                return self._pause_uncertain(
                    run_id,
                    uncertain,
                    started,
                    cancellation_requested=context_cancelled(context),
                )
            if not unfinished:
                break
            if not progress:
                return self._fail_run(
                    run_id,
                    FailureDetail(
                        code="scheduler_stalled",
                        message=(
                            "No pending step could be scheduled from the "
                            "persisted dependency graph."
                        ),
                    ),
                    started,
                )

        required_failures = [
            step
            for step in plan.steps
            if step.required and steps[step.step_key].status != StepStatus.SUCCEEDED
        ]
        with self._repository.transaction():
            current = self._get_run(run_id)
            if context_cancelled(context):
                self._set_run_error(
                    current, "user_cancelled", "Execution was cancelled by the caller."
                )
                self._repository.transition_run(
                    run_id, RunStatus.CANCELLED, details={"reason": "user_requested"}
                )
                self._repository.create_event(
                    RunEvent(
                        run_id=run_id,
                        event_type="run.cancelled",
                        details={"reason": "user_requested"},
                    )
                )
            else:
                self._repository.transition_run(run_id, RunStatus.VALIDATING)
                if required_failures:
                    primary = next(
                        (
                            failures[step.step_key]
                            for step in required_failures
                            if step.step_key in failures
                        ),
                        FailureDetail(
                            code="required_step_incomplete",
                            message="A required workflow step did not succeed.",
                            step_key=required_failures[0].step_key,
                        ),
                    )
                    validating = self._get_run(run_id)
                    self._set_run_error(validating, primary.code, primary.message)
                    self._repository.transition_run(run_id, RunStatus.FAILED)
                else:
                    if failures:
                        self._repository.create_event(
                            RunEvent(
                                run_id=run_id,
                                event_type="run.optional_steps_failed",
                                details={"step_keys": sorted(failures)},
                            )
                        )
                    self._repository.transition_run(
                        run_id,
                        RunStatus.SUCCEEDED,
                        details={"optional_failures": len(failures)},
                    )
        return self._result(run_id, time.monotonic() - started)

    def _execute_step(
        self,
        run_id: UUID,
        step_key: str,
        tool_name: str,
        dependencies: list[str],
        step: WorkflowStep,
        outputs: dict[str, dict[str, Any]],
        context: ToolExecutionContext,
        retryable: bool,
        tool_version: str,
    ) -> tuple[WorkflowStep, dict[str, Any] | None, FailureDetail | None, bool]:
        resolved = resolve_step_arguments(
            step.arguments,
            current_step_key=step_key,
            direct_dependencies=set(dependencies),
            successful_outputs=outputs,
        )
        input_hash = hashlib.sha256(_canonical_json(resolved)).hexdigest()
        current_step = step
        attempts = self._repository.list_attempts(step.id)
        for retry_index in range(self._retry_policy.max_retries + 1):
            if context_cancelled(context):
                return (
                    self._transition_step(
                        current_step, StepStatus.CANCELLED, {"reason": "user_requested"}
                    ),
                    None,
                    None,
                    False,
                )
            attempt_number = len(attempts) + 1
            attempt = StepAttempt(
                step_id=step.id, attempt_number=attempt_number, input_hash=input_hash
            )
            with self._repository.transaction():
                if current_step.status in {StepStatus.PENDING, StepStatus.FAILED}:
                    current_step = self._repository.transition_step(
                        current_step.id, StepStatus.READY
                    )
                current_step = self._repository.transition_step(
                    current_step.id,
                    StepStatus.RUNNING,
                    details={"tool_name": tool_name, "attempt_number": attempt_number},
                )
                self._repository.create_attempt(attempt)

            attempt_started = time.monotonic()
            error: ToolError | None = None
            try:
                tool_output = self._registry.invoke(
                    tool_name, resolved, context=context, version=tool_version
                )
            except ToolError as tool_error:
                error = tool_error
            except Exception:
                error = ToolError(
                    "Tool registry invocation failed",
                    tool_name=tool_name,
                    tool_version=tool_version,
                    code="registry_failure",
                )
            else:
                output_data = tool_output.model_dump(mode="json")
                try:
                    persisted_output = ToolResult(
                        success=True,
                        data=output_data,
                        summary=f"Tool {tool_name} completed successfully.",
                    )
                except Exception:
                    failure = FailureDetail(
                        code="output_persistence_invalid",
                        message=(
                            "Validated tool output could not be stored within the result limit."
                        ),
                        step_key=step_key,
                        attempt_number=attempt_number,
                    )
                    current_step = self._mark_attempt_uncertain(
                        run_id,
                        step_key,
                        tool_name,
                        input_hash,
                        current_step,
                        attempt,
                        attempt_number,
                        failure,
                        attempt_started,
                    )
                    return current_step, None, failure, True

                completed_attempt = StepAttempt.model_validate(
                    {
                        **attempt.model_dump(),
                        "status": AttemptStatus.SUCCEEDED,
                        "output": persisted_output,
                        "duration_ms": max(0, int((time.monotonic() - attempt_started) * 1000)),
                        "finished_at": utc_now(),
                    }
                )
                with self._repository.transaction():
                    self._repository.update_attempt(completed_attempt)
                    current_step = self._repository.transition_step(
                        current_step.id,
                        StepStatus.SUCCEEDED,
                        details={"tool_name": tool_name, "attempt_number": attempt_number},
                    )
                    self._repository.create_event(
                        RunEvent(
                            run_id=run_id,
                            event_type="step.attempt.succeeded",
                            details={
                                "step_key": step_key,
                                "tool_name": tool_name,
                                "tool_version": tool_version,
                                "attempt_number": attempt_number,
                                "duration_ms": completed_attempt.duration_ms or 0,
                                "input_hash": input_hash,
                            },
                        )
                    )
                return current_step, output_data, None, False

            if error is None:
                raise RuntimeError("Tool invocation completed without a result or error")
            current_step, failure = self._record_attempt_failure(
                run_id,
                step_key,
                tool_name,
                input_hash,
                current_step,
                attempt,
                attempt_number,
                error,
                attempt_started,
            )
            if isinstance(error, ToolTimeoutError):
                return current_step, None, failure, True
            if error.code == "cancelled" and context_cancelled(context):
                current_step = self._transition_step(
                    current_step, StepStatus.CANCELLED, {"reason": "user_requested"}
                )
                return current_step, None, failure, False
            can_retry = (
                retryable
                and retry_index < self._retry_policy.max_retries
                and isinstance(error, (ToolBusyError, ToolTransientError))
            )
            if not can_retry:
                return current_step, None, failure, False
            delay = self._retry_policy.delay_for_retry(retry_index + 1)
            if delay and self._wait(delay, context._cancelled):
                current_step = self._transition_step(
                    current_step, StepStatus.CANCELLED, {"reason": "user_requested"}
                )
                return current_step, None, failure, False
            attempts.append(attempt)
        return (
            current_step,
            None,
            FailureDetail(
                code="retry_exhausted",
                message="The bounded retry limit was reached.",
                step_key=step_key,
            ),
            False,
        )

    def _mark_attempt_uncertain(
        self,
        run_id: UUID,
        step_key: str,
        tool_name: str,
        input_hash: str,
        step: WorkflowStep,
        attempt: StepAttempt,
        attempt_number: int,
        failure: FailureDetail,
        attempt_started: float,
    ) -> WorkflowStep:
        completed_attempt = StepAttempt.model_validate(
            {
                **attempt.model_dump(),
                "status": AttemptStatus.FAILED,
                "error_code": failure.code,
                "error_summary": failure.message,
                "duration_ms": max(0, int((time.monotonic() - attempt_started) * 1000)),
                "finished_at": utc_now(),
            }
        )
        with self._repository.transaction():
            self._repository.update_attempt(completed_attempt)
            step = self._repository.transition_step(
                step.id,
                StepStatus.FAILED,
                details={"tool_name": tool_name, "attempt_number": attempt_number},
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type="step.attempt.uncertain",
                    details={
                        "step_key": step_key,
                        "tool_name": tool_name,
                        "attempt_number": attempt_number,
                        "error_code": failure.code,
                        "input_hash": input_hash,
                    },
                )
            )
        return step

    def _record_reference_failure(
        self, run_id: UUID, step_key: str, tool_name: str, step: WorkflowStep
    ) -> tuple[WorkflowStep, FailureDetail]:
        """Persist a reference-resolution failure without invoking the downstream tool."""
        attempt_number = len(self._repository.list_attempts(step.id)) + 1
        input_hash = hashlib.sha256(_canonical_json(step.arguments)).hexdigest()
        attempt = StepAttempt(
            step_id=step.id,
            attempt_number=attempt_number,
            input_hash=input_hash,
        )
        message = "A step output reference was malformed, unavailable, or outside its dependencies."
        completed_attempt = StepAttempt.model_validate(
            {
                **attempt.model_dump(),
                "status": AttemptStatus.FAILED,
                "error_code": "invalid_output_reference",
                "error_summary": message,
                "duration_ms": 0,
                "finished_at": utc_now(),
            }
        )
        with self._repository.transaction():
            if step.status in {StepStatus.PENDING, StepStatus.FAILED}:
                step = self._repository.transition_step(step.id, StepStatus.READY)
            step = self._repository.transition_step(
                step.id,
                StepStatus.RUNNING,
                details={"tool_name": tool_name, "attempt_number": attempt_number},
            )
            self._repository.create_attempt(attempt)
            self._repository.update_attempt(completed_attempt)
            step = self._repository.transition_step(
                step.id,
                StepStatus.FAILED,
                details={
                    "tool_name": tool_name,
                    "attempt_number": attempt_number,
                    "error_code": "invalid_output_reference",
                },
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type="step.reference.failed",
                    details={
                        "step_key": step_key,
                        "tool_name": tool_name,
                        "attempt_number": attempt_number,
                        "error_code": "invalid_output_reference",
                    },
                )
            )
        return step, FailureDetail(
            code="invalid_output_reference",
            message=message,
            step_key=step_key,
            attempt_number=attempt_number,
        )

    def _record_attempt_failure(
        self,
        run_id: UUID,
        step_key: str,
        tool_name: str,
        input_hash: str,
        step: WorkflowStep,
        attempt: StepAttempt,
        attempt_number: int,
        error: ToolError,
        attempt_started: float,
    ) -> tuple[WorkflowStep, FailureDetail]:
        code, message = _safe_failure(error)
        completed_attempt = StepAttempt.model_validate(
            {
                **attempt.model_dump(),
                "status": AttemptStatus.FAILED,
                "error_code": code,
                "error_summary": message,
                "duration_ms": max(0, int((time.monotonic() - attempt_started) * 1000)),
                "finished_at": utc_now(),
            }
        )
        with self._repository.transaction():
            self._repository.update_attempt(completed_attempt)
            step = self._repository.transition_step(
                step.id,
                StepStatus.FAILED,
                details={
                    "tool_name": tool_name,
                    "attempt_number": attempt_number,
                    "error_code": code,
                },
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type="step.attempt.failed",
                    details={
                        "step_key": step_key,
                        "tool_name": tool_name,
                        "attempt_number": attempt_number,
                        "error_code": code,
                        "duration_ms": completed_attempt.duration_ms or 0,
                        "input_hash": input_hash,
                    },
                )
            )
        return step, FailureDetail(
            code=code,
            message=message,
            step_key=step_key,
            attempt_number=attempt_number,
        )

    def _approval_decision(
        self, run_id: UUID, step_id: UUID | None, digest: str
    ) -> ApprovalDecision:
        matching = [
            approval
            for approval in self._repository.list_approvals(run_id)
            if approval.run_id == run_id
            and approval.step_id == step_id
            and approval.plan_digest == digest
        ]
        if any(
            item.status == ApprovalStatus.APPROVED and item.resolved_at is not None
            for item in matching
        ):
            return "approved"
        if any(
            item.status in {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED} for item in matching
        ):
            return "rejected"
        return "pending"

    def _pause_for_approval(
        self, run_id: UUID, scope: str, step_id: UUID | None, digest: str
    ) -> None:
        with self._repository.transaction():
            approvals = self._repository.list_approvals(run_id)
            pending_exists = any(
                item.run_id == run_id
                and item.step_id == step_id
                and item.plan_digest == digest
                and item.status == ApprovalStatus.PENDING
                for item in approvals
            )
            if not pending_exists:
                self._repository.create_approval(
                    ApprovalRequest(run_id=run_id, step_id=step_id, plan_digest=digest)
                )
            current = self._get_run(run_id)
            if current.status != RunStatus.AWAITING_APPROVAL:
                self._repository.transition_run(
                    run_id,
                    RunStatus.AWAITING_APPROVAL,
                    details={"scope": scope},
                )

    def _pause_uncertain(
        self,
        run_id: UUID,
        failure: FailureDetail | None,
        started: float,
        *,
        cancellation_requested: bool = False,
    ) -> ExecutionResult:
        safe_failure = failure or FailureDetail(
            code="execution_uncertain",
            message="A tool outcome is uncertain; automatic replay is prohibited.",
        )
        error_code = (
            "user_cancel_outcome_uncertain" if cancellation_requested else "tool_outcome_uncertain"
        )
        event_type = "run.cancel_uncertain" if cancellation_requested else "run.execution_uncertain"
        with self._repository.transaction():
            current = self._get_run(run_id)
            self._set_run_error(current, error_code, safe_failure.message)
            self._repository.transition_run(
                run_id,
                RunStatus.PAUSED,
                details={"reason": error_code, "replay": "prohibited"},
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type=event_type,
                    details={"error_code": error_code, "replay": "prohibited"},
                )
            )
        return self._result(run_id, time.monotonic() - started, failure=safe_failure)

    def _fail_run(self, run_id: UUID, failure: FailureDetail, started: float) -> ExecutionResult:
        with self._repository.transaction():
            current = self._get_run(run_id)
            if current.status not in {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.SUCCEEDED}:
                self._set_run_error(current, failure.code, failure.message)
                self._repository.transition_run(
                    run_id,
                    RunStatus.FAILED,
                    details={"error_code": failure.code},
                )
        return self._result(run_id, time.monotonic() - started, failure=failure)

    def _finish_cancelled(
        self, run_id: UUID, failures: dict[str, FailureDetail], started: float
    ) -> ExecutionResult:
        with self._repository.transaction():
            current = self._get_run(run_id)
            if current.status not in {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.SUCCEEDED}:
                self._set_run_error(
                    current, "user_cancelled", "Execution was cancelled by the caller."
                )
                self._repository.transition_run(
                    run_id,
                    RunStatus.CANCELLED,
                    details={"reason": "user_requested"},
                )
                self._repository.create_event(
                    RunEvent(
                        run_id=run_id,
                        event_type="run.cancelled",
                        details={"reason": "user_requested"},
                    )
                )
        return self._result(run_id, time.monotonic() - started)

    def _cancel_remaining(self, run_id: UUID, steps: dict[str, WorkflowStep]) -> None:
        with self._repository.transaction():
            for key, step in tuple(steps.items()):
                if step.status in {StepStatus.PENDING, StepStatus.READY}:
                    steps[key] = self._repository.transition_step(
                        step.id, StepStatus.CANCELLED, details={"reason": "user_requested"}
                    )

    def _transition_step(
        self, step: WorkflowStep, target: StepStatus, details: dict[str, Any] | None = None
    ) -> WorkflowStep:
        with self._repository.transaction():
            return self._repository.transition_step(step.id, target, details=details)

    def _set_run_error(self, current: WorkflowRun, code: str, message: str) -> None:
        self._repository.update_run(
            WorkflowRun.model_validate(
                {
                    **current.model_dump(),
                    "error_code": code,
                    "error_summary": message[:4000],
                }
            )
        )

    def _load_and_validate_steps(
        self, run: WorkflowRun, plan: ExecutionPlan
    ) -> dict[str, WorkflowStep]:
        persisted_steps = self._repository.list_steps(run.id)
        if len(persisted_steps) != len(plan.steps):
            raise InvalidWorkflowPlanError("Persisted workflow step count does not match its plan")
        indexed = {step.step_key: step for step in persisted_steps}
        if len(indexed) != len(plan.steps):
            raise InvalidWorkflowPlanError("Persisted workflow steps contain duplicate keys")
        for position, plan_step in enumerate(plan.steps):
            step = indexed.get(plan_step.step_key)
            if step is None or (
                step.run_id != run.id
                or step.position != position
                or step.tool_name != plan_step.tool_name
                or step.arguments != plan_step.arguments
                or step.depends_on != plan_step.depends_on
                or step.required != plan_step.required
            ):
                raise InvalidWorkflowPlanError(
                    "Persisted workflow step differs from the validated plan"
                )
            if step.tool_version is None:
                raise InvalidWorkflowPlanError(
                    f"Persisted step {plan_step.step_key!r} has no pinned tool version"
                )
            try:
                self._registry.get(plan_step.tool_name, step.tool_version)
            except ToolError as error:
                raise InvalidWorkflowPlanError(
                    f"Pinned tool version is unavailable for step {plan_step.step_key!r}"
                ) from error
        return indexed

    def _restore_successful_outputs(
        self, steps: dict[str, WorkflowStep]
    ) -> dict[str, dict[str, Any]]:
        outputs: dict[str, dict[str, Any]] = {}
        for step_key, step in steps.items():
            if step.status != StepStatus.SUCCEEDED:
                continue
            attempts = self._repository.list_attempts(step.id)
            successful = next(
                (item for item in reversed(attempts) if item.status == AttemptStatus.SUCCEEDED),
                None,
            )
            if successful is None or successful.output is None or not successful.output.success:
                raise InvalidWorkflowPlanError(
                    f"Succeeded step {step_key!r} has no validated persisted output"
                )
            outputs[step_key] = successful.output.data
        return outputs

    def _restore_failures(self, steps: dict[str, WorkflowStep]) -> dict[str, FailureDetail]:
        failures: dict[str, FailureDetail] = {}
        for step_key, step in steps.items():
            if step.status != StepStatus.FAILED:
                continue
            attempts = self._repository.list_attempts(step.id)
            failed = next(
                (item for item in reversed(attempts) if item.status == AttemptStatus.FAILED),
                None,
            )
            failures[step_key] = FailureDetail(
                code=failed.error_code if failed and failed.error_code else "step_failed",
                message=(
                    failed.error_summary
                    if failed and failed.error_summary
                    else "Step did not complete successfully."
                ),
                step_key=step_key,
                attempt_number=failed.attempt_number if failed else None,
            )
        return failures

    def _result(
        self,
        run_id: UUID,
        duration_seconds: float,
        *,
        failure: FailureDetail | None = None,
    ) -> ExecutionResult:
        run = self._get_run(run_id)
        results: list[StepExecutionResult] = []
        for step in self._repository.list_steps(run_id):
            attempts = self._repository.list_attempts(step.id)
            latest_success = next(
                (item for item in reversed(attempts) if item.status == AttemptStatus.SUCCEEDED),
                None,
            )
            latest_failure = next(
                (item for item in reversed(attempts) if item.status == AttemptStatus.FAILED),
                None,
            )
            step_failure = None
            if step.status in {StepStatus.FAILED, StepStatus.CANCELLED} and latest_failure:
                step_failure = FailureDetail(
                    code=latest_failure.error_code or "step_failed",
                    message=latest_failure.error_summary or "Step did not complete successfully.",
                    step_key=step.step_key,
                    attempt_number=latest_failure.attempt_number,
                )
            results.append(
                StepExecutionResult(
                    step_key=step.step_key,
                    tool_name=step.tool_name,
                    status=step.status,
                    attempts=len(attempts),
                    output=latest_success.output.data
                    if latest_success and latest_success.output
                    else None,
                    failure=step_failure,
                    duration_ms=sum(item.duration_ms or 0 for item in attempts),
                )
            )
        return ExecutionResult(
            run_id=run_id,
            status=run.status,
            steps=results,
            failure=failure
            or (
                FailureDetail(code=run.error_code, message=run.error_summary)
                if run.error_code and run.error_summary
                else None
            ),
            duration_ms=max(0, int(duration_seconds * 1000)),
            events=self._repository.list_events(run_id, limit=500),
        )

    def _get_run(self, run_id: UUID) -> WorkflowRun:
        run = self._repository.get_run(run_id)
        if run is None:
            raise RunNotFoundError(f"Workflow run {run_id} was not found")
        return run


def context_cancelled(context: ToolExecutionContext) -> bool:
    """Check the context-owned cancellation signal without accepting caller flags."""
    try:
        context.checkpoint()
    except ToolError as error:
        return error.code == "cancelled"
    return False


def _validated_plan_snapshot(plan: ExecutionPlan) -> ExecutionPlan:
    try:
        return ExecutionPlan.model_validate_json(plan.model_dump_json())
    except Exception as error:
        raise InvalidWorkflowPlanError("Workflow plan failed snapshot validation") from error


def _digest_plan(plan: ExecutionPlan) -> str:
    return hashlib.sha256(_canonical_json(plan.model_dump(mode="json"))).hexdigest()


def _digest_step(plan_digest: str, step: Any, version: str) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "plan_digest": plan_digest,
                "step_key": step.step_key,
                "tool_name": step.tool_name,
                "tool_version": version,
                "arguments": step.arguments,
            }
        )
    ).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_failure(error: ToolError) -> tuple[str, str]:
    if isinstance(error, ToolInputError):
        return error.code, "Tool input validation failed."
    if isinstance(error, ToolPermissionError):
        return error.code, "Tool permission check failed."
    if isinstance(error, ToolApprovalRequired):
        return error.code, "A required persisted approval was not available."
    if isinstance(error, ToolTimeoutError):
        return error.code, "Tool timed out; its side-effect outcome may be uncertain."
    if isinstance(error, ToolTransientError):
        return error.code, "Tool reported a transient failure before committing side effects."
    if isinstance(error, ToolBusyError):
        return error.code, "Tool did not start because execution capacity was unavailable."
    if isinstance(error, ToolOutputError):
        return error.code, "Tool output failed validation or persistence limits."
    return error.code, "Tool execution failed."


def _interruptible_wait(delay: float, cancellation: threading.Event) -> bool:
    """Wait for backoff, returning true if cancellation interrupted the delay."""
    return cancellation.wait(delay)
