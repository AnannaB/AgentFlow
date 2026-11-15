"""Typed workflow execution failures safe to present to application callers."""


class WorkflowExecutionError(RuntimeError):
    """Base class for workflow service errors."""


class InvalidWorkflowPlanError(WorkflowExecutionError):
    """Raised when a plan snapshot cannot be revalidated or scheduled."""


class InvalidOutputReferenceError(WorkflowExecutionError):
    """Raised when a reserved output-reference marker is malformed or unresolved."""


class RunNotFoundError(WorkflowExecutionError):
    """Raised when a requested workflow run does not exist."""


class RunNotExecutableError(WorkflowExecutionError):
    """Raised when a run is terminal, interrupted, or already executing."""


class ApprovalError(WorkflowExecutionError):
    """Base error for approval management failures."""


class ApprovalNotFoundError(ApprovalError):
    """Raised when the specified approval does not exist."""


class ApprovalScopeError(ApprovalError):
    """Raised when approval run, step, purpose, or digest does not match."""


class ApprovalStateError(ApprovalError):
    """Raised when an approval is not pending or its run is in the wrong state."""


class ReportError(WorkflowExecutionError):
    """Base error for report generation or export failures."""


class ReportNotFoundError(ReportError):
    """Raised when the source workflow run does not exist."""


class ReportNotReadyError(ReportError):
    """Raised when the workflow has not completed successfully or failed."""


class ReportExportError(ReportError):
    """Raised when a validated report cannot be exported safely."""


class ReportTooLargeError(ReportExportError):
    """Raised when the report exceeds bounded exporter input size."""
