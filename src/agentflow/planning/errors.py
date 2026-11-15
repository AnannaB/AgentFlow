"""Typed, sanitized errors from local planning components."""


class PlanningError(RuntimeError):
    """Base class for planning failures safe to present to callers."""


class OllamaConnectionError(PlanningError):
    """Ollama could not be reached or did not return a response in time."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str = "ollama_connection_error",
        os_error_code: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.os_error_code = os_error_code
        self.http_status = http_status


class OllamaResponseError(PlanningError):
    """Ollama returned a malformed or incomplete API response."""

    def __init__(self, message: str, *, diagnostic_code: str = "ollama_response_invalid") -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


class PlanValidationError(PlanningError):
    """A proposed plan violates schema, registry, or policy constraints."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str = "plan_invalid",
        step_index: int | None = None,
        tool_name: str | None = None,
        field_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.step_index = step_index
        self.tool_name = tool_name
        self.field_path = field_path


class FallbackUnavailableError(PlanningError):
    """No safe deterministic rule can handle the objective."""
