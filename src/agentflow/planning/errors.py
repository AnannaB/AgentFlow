"""Typed, sanitized errors from local planning components."""


class PlanningError(RuntimeError):
    """Base class for planning failures safe to present to callers."""


class OllamaConnectionError(PlanningError):
    """Ollama could not be reached or did not return a response in time."""


class OllamaResponseError(PlanningError):
    """Ollama returned a malformed or incomplete API response."""


class PlanValidationError(PlanningError):
    """A proposed plan violates schema, registry, or policy constraints."""


class FallbackUnavailableError(PlanningError):
    """No safe deterministic rule can handle the objective."""
