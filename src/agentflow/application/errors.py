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
