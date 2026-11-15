"""Local Ollama model discovery for the dashboard settings page."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentflow.config import Settings

MAX_TAGS_RESPONSE_BYTES = 1024 * 1024


class OllamaDiagnosticError(RuntimeError):
    """A concise, safe error from the local Ollama connection test."""


def list_local_models(
    settings: Settings, *, opener: Callable[..., object] = urlopen
) -> tuple[str, ...]:
    """Query the local Ollama tags endpoint without logging request details."""
    request = Request(f"{settings.ollama_base_url}/api/tags", method="GET")
    try:
        with opener(request, timeout=settings.ollama_timeout_seconds) as response:
            body = response.read(MAX_TAGS_RESPONSE_BYTES + 1)
    except TimeoutError:
        raise OllamaDiagnosticError("Ollama connection timed out.") from None
    except HTTPError as error:
        error.close()
        raise OllamaDiagnosticError(f"Ollama returned HTTP {error.code}.") from None
    except (URLError, OSError):
        raise OllamaDiagnosticError(
            "Could not connect to Ollama at the configured local address."
        ) from None
    if len(body) > MAX_TAGS_RESPONSE_BYTES:
        raise OllamaDiagnosticError("Ollama model list exceeded the 1 MiB response limit.")
    try:
        payload = json.loads(body)
        models = payload["models"]
        names = tuple(item["name"] for item in models)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise OllamaDiagnosticError("Ollama returned an invalid model list.") from None
    if not all(isinstance(name, str) and name for name in names):
        raise OllamaDiagnosticError("Ollama returned an invalid model list.")
    return names
