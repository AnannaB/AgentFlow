"""Transactional approval decisions for workflow steps and report exports."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from agentflow.application.errors import (
    ApprovalNotFoundError,
    ApprovalScopeError,
    ApprovalStateError,
)
from agentflow.application.execution import WorkflowExecutionService
from agentflow.domain.models import ApprovalRequest, RunEvent, utc_now
from agentflow.domain.states import ApprovalStatus, RunStatus
from agentflow.storage.protocols import WorkflowRepository

ApprovalPurpose = Literal["workflow", "report_export"]
ApprovalDecision = Literal["approved", "rejected"]


class ApprovalService:
    """List pending requests and resolve only exact persisted approval scopes."""

    def __init__(self, repository: WorkflowRepository, execution: WorkflowExecutionService) -> None:
        self._repository = repository
        self._execution = execution

    def list_pending(
        self, *, run_id: UUID | None = None, limit: int = 100
    ) -> tuple[ApprovalRequest, ...]:
        """Return a bounded, stable list of pending approvals."""
        if run_id is None:
            items = self._repository.list_pending_approvals(limit=limit)
        else:
            items = [
                item
                for item in self._repository.list_approvals(run_id, limit=limit)
                if item.status == ApprovalStatus.PENDING
            ][:limit]
        return tuple(items)

    def approve(
        self,
        approval_id: UUID,
        *,
        run_id: UUID,
        step_id: UUID | None,
        plan_digest: str,
        decision_note: str | None = None,
    ) -> ApprovalRequest:
        """Approve a pending request after validating exact scope and digest."""
        return self._decide(
            approval_id,
            decision=ApprovalStatus.APPROVED,
            run_id=run_id,
            step_id=step_id,
            plan_digest=plan_digest,
            decision_note=decision_note,
        )

    def reject(
        self,
        approval_id: UUID,
        *,
        run_id: UUID,
        step_id: UUID | None,
        plan_digest: str,
        decision_note: str | None = None,
    ) -> ApprovalRequest:
        """Reject a pending request after validating exact scope and digest."""
        return self._decide(
            approval_id,
            decision=ApprovalStatus.REJECTED,
            run_id=run_id,
            step_id=step_id,
            plan_digest=plan_digest,
            decision_note=decision_note,
        )

    def request_report_export(self, run_id: UUID, digest: str) -> ApprovalRequest:
        """Persist a report-export authorization request for a terminal run."""
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ApprovalScopeError("Report export digest must be a lowercase SHA-256 value")
        with self._repository.transaction():
            run = self._repository.get_run(run_id)
            if run is None:
                raise ApprovalNotFoundError("Workflow run was not found")
            if run.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
                raise ApprovalStateError(
                    "Reports can be exported only for completed or failed runs"
                )
            existing = self._repository.list_approvals(run_id)
            for request in reversed(existing):
                if (
                    request.purpose == "report_export"
                    and request.step_id is None
                    and request.plan_digest == digest
                    and request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                ):
                    return request
            request = self._repository.create_approval(
                ApprovalRequest(
                    run_id=run_id,
                    purpose="report_export",
                    plan_digest=digest,
                )
            )
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type="approval.requested",
                    details={
                        "approval_id": str(request.id),
                        "purpose": "report_export",
                    },
                )
            )
            return request

    def verify_report_export(
        self, approval_id: UUID, *, run_id: UUID, digest: str
    ) -> ApprovalRequest:
        """Verify persisted approval before granting the JSON export tool gate."""
        request = self._repository.get_approval(approval_id)
        if (
            request is None
            or request.run_id != run_id
            or request.step_id is not None
            or request.purpose != "report_export"
            or request.plan_digest != digest
        ):
            raise ApprovalScopeError("Report export approval scope or digest does not match")
        if request.status != ApprovalStatus.APPROVED or request.resolved_at is None:
            raise ApprovalStateError("A resolved approved export request is required")
        return request

    def _decide(
        self,
        approval_id: UUID,
        *,
        decision: ApprovalStatus,
        run_id: UUID,
        step_id: UUID | None,
        plan_digest: str,
        decision_note: str | None,
    ) -> ApprovalRequest:
        with self._repository.transaction():
            request = self._repository.get_approval(approval_id)
            if request is None:
                raise ApprovalNotFoundError("Approval request was not found")
            if (
                request.run_id != run_id
                or request.step_id != step_id
                or request.plan_digest != plan_digest
            ):
                raise ApprovalScopeError("Approval run, step, or digest does not match")
            if request.status != ApprovalStatus.PENDING:
                raise ApprovalStateError("Only a pending approval can be resolved")
            run = self._repository.get_run(run_id)
            if run is None:
                raise ApprovalNotFoundError("Workflow run was not found")

            if request.purpose == "workflow":
                if run.status != RunStatus.AWAITING_APPROVAL:
                    raise ApprovalStateError("Workflow run is not awaiting approval")
                try:
                    expected_digest = self._execution.approval_digest(run_id, step_id)
                except Exception as error:
                    raise ApprovalScopeError(
                        "Approval scope is not required by this workflow"
                    ) from error
                if expected_digest != plan_digest:
                    raise ApprovalScopeError("Approval digest is stale or does not match the plan")
            elif request.purpose == "report_export":
                if step_id is not None or run.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
                    raise ApprovalStateError("Report export approval requires a completed workflow")
            else:
                raise ApprovalScopeError("Unsupported approval purpose")

            resolved = ApprovalRequest.model_validate(
                {
                    **request.model_dump(),
                    "status": decision,
                    "decision_note": decision_note,
                    "resolved_at": utc_now(),
                }
            )
            self._repository.update_approval(resolved)
            self._repository.create_event(
                RunEvent(
                    run_id=run_id,
                    event_type=f"approval.{decision.value}",
                    details={
                        "approval_id": str(approval_id),
                        "purpose": request.purpose,
                        "step_id": str(step_id) if step_id else None,
                    },
                )
            )
            return resolved
