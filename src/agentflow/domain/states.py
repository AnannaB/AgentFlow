"""Workflow and step lifecycle states."""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VALIDATING = "validating"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_STEP_STATUSES = frozenset({StepStatus.SUCCEEDED, StepStatus.SKIPPED, StepStatus.CANCELLED})


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PlannerKind(StrEnum):
    OLLAMA = "ollama"
    RULE_BASED = "rule_based"
