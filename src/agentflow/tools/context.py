"""Per-invocation execution context with bounded time and cancellation checks."""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agentflow.tools.errors import ToolExecutionError


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Caller-provided workspace, approval flag, and cooperative cancellation state."""

    workspace_roots: tuple[Path | str, ...] = ()
    approved: bool = False
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    _cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    _parent_cancelled: threading.Event | None = field(default=None, repr=False)
    _deadline: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        roots: list[Path] = []
        for item in self.workspace_roots:
            root = Path(item).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError("each workspace root must be an existing directory")
            if root not in roots:
                roots.append(root)
        object.__setattr__(self, "workspace_roots", tuple(roots))
        if type(self.approved) is not bool:
            raise TypeError("approved must be a bool")
        if not self.correlation_id or len(self.correlation_id) > 128:
            raise ValueError("correlation_id must contain 1 to 128 characters")

    def with_timeout(self, timeout_seconds: float) -> "ToolExecutionContext":
        """Create an invocation context that also observes the caller's cancellation."""
        deadline = time.monotonic() + timeout_seconds
        if self._deadline is not None:
            deadline = min(deadline, self._deadline)
        return ToolExecutionContext(
            workspace_roots=self.workspace_roots,
            approved=self.approved,
            correlation_id=self.correlation_id,
            _parent_cancelled=self._cancelled,
            _deadline=deadline,
        )

    def with_approval(self) -> "ToolExecutionContext":
        """Create an approved child sharing this context's cancellation signal.

        Callers must derive this context only after verifying an authoritative
        approval record. The registry treats the flag as a gate, not as proof.
        """
        return ToolExecutionContext(
            workspace_roots=self.workspace_roots,
            approved=True,
            correlation_id=self.correlation_id,
            _cancelled=self._cancelled,
            _parent_cancelled=self._parent_cancelled,
            _deadline=self._deadline,
        )

    def cancel(self) -> None:
        """Signal cooperative cancellation to the active tool implementation."""
        self._cancelled.set()

    def checkpoint(self, tool_name: str | None = None) -> None:
        """Raise if cancellation or the invocation deadline has been reached."""
        if self._cancelled.is_set() or (
            self._parent_cancelled is not None and self._parent_cancelled.is_set()
        ):
            raise ToolExecutionError(
                "Tool execution was cancelled",
                tool_name=tool_name or "unknown",
                tool_version="unknown",
                code="cancelled",
            )
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise ToolExecutionError(
                "Tool execution deadline has expired",
                tool_name=tool_name or "unknown",
                tool_version="unknown",
                code="timeout",
            )
