"""Typed failures raised at the tool boundary."""

from typing import Any


class ToolError(RuntimeError):
    """Base class for safe, user-presentable tool failures."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        tool_version: str | None = None,
        code: str = "tool_error",
    ) -> None:
        self.tool_name = tool_name
        self.tool_version = tool_version
        self.code = code
        super().__init__(message)


class DuplicateToolError(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            f"Tool {name!r} version {version} is already registered",
            tool_name=name,
            tool_version=version,
            code="duplicate_tool",
        )


class UnknownToolError(ToolError):
    def __init__(self, name: str, version: str | None = None) -> None:
        suffix = f" version {version}" if version else ""
        super().__init__(
            f"Unknown tool {name!r}{suffix}",
            tool_name=name,
            tool_version=version,
            code="unknown_tool",
        )


class ToolInputError(ToolError):
    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        tool_version: str | None = None,
        issues: tuple[dict[str, Any], ...] = (),
        code: str = "invalid_input",
    ) -> None:
        self.issues = issues
        super().__init__(
            message,
            tool_name=tool_name,
            tool_version=tool_version,
            code=code,
        )


class ToolOutputError(ToolError):
    def __init__(self, message: str, *, tool_name: str, tool_version: str) -> None:
        super().__init__(
            message,
            tool_name=tool_name,
            tool_version=tool_version,
            code="invalid_output",
        )


class ToolExecutionError(ToolError):
    def __init__(
        self,
        message: str,
        *,
        tool_name: str,
        tool_version: str,
        code: str = "execution_failed",
    ) -> None:
        super().__init__(
            message,
            tool_name=tool_name,
            tool_version=tool_version,
            code=code,
        )


class ToolTransientError(ToolError):
    """A registered tool reports a transient failure known to precede side effects."""

    def __init__(self, message: str, *, tool_name: str, tool_version: str) -> None:
        super().__init__(
            message,
            tool_name=tool_name,
            tool_version=tool_version,
            code="transient_failure",
        )


class ToolTimeoutError(ToolError):
    def __init__(self, name: str, version: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Tool {name!r} exceeded its {timeout_seconds:g}s time limit",
            tool_name=name,
            tool_version=version,
            code="timeout",
        )


class ToolBusyError(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            f"No execution capacity is available for tool {name!r}",
            tool_name=name,
            tool_version=version,
            code="capacity_exhausted",
        )


class ToolApprovalRequired(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            f"Tool {name!r} requires explicit approval",
            tool_name=name,
            tool_version=version,
            code="approval_required",
        )


class ToolPermissionError(ToolError):
    def __init__(self, message: str, *, tool_name: str | None = None) -> None:
        super().__init__(message, tool_name=tool_name, code="permission_denied")
