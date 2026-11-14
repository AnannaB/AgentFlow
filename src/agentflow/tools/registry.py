"""Thread-safe registry for validated, explicitly implemented tools."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from agentflow.tools.base import ToolDefinition, ToolInput, ToolMetadata, ToolOutput
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import (
    DuplicateToolError,
    ToolApprovalRequired,
    ToolBusyError,
    ToolError,
    ToolExecutionError,
    ToolInputError,
    ToolOutputError,
    ToolTimeoutError,
    UnknownToolError,
)

MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Safe discovery representation; contains schemas but no callable."""

    metadata: ToolMetadata
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class ToolRegistry:
    """Registers versioned tools and validates every invocation boundary."""

    def __init__(self, *, max_concurrent: int = 8) -> None:
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 128:
            raise ValueError("max_concurrent must be an integer from 1 to 128")
        self._tools: dict[str, dict[str, ToolDefinition[Any, Any]]] = {}
        self._lock = threading.RLock()
        self._capacity = threading.BoundedSemaphore(max_concurrent)

    def register(self, definition: ToolDefinition[Any, Any]) -> None:
        """Register a name/version pair; the same pair can never be replaced."""
        name = definition.metadata.name
        version = definition.metadata.version
        with self._lock:
            versions = self._tools.setdefault(name, {})
            if version in versions:
                raise DuplicateToolError(name, version)
            versions[version] = definition

    def get(self, name: str, version: str | None = None) -> ToolDefinition[ToolInput, ToolOutput]:
        """Return the requested version, or the latest registered version."""
        with self._lock:
            versions = self._tools.get(name)
            if not versions:
                raise UnknownToolError(name, version)
            selected = version or max(versions, key=_version_key)
            definition = versions.get(selected)
            if definition is None:
                raise UnknownToolError(name, version)
            return definition  # type: ignore[return-value]

    def versions(self, name: str) -> tuple[str, ...]:
        """List all registered versions of one tool in semantic version order."""
        with self._lock:
            versions = self._tools.get(name)
            if not versions:
                raise UnknownToolError(name)
            return tuple(sorted(versions, key=_version_key))

    def discover(self, *, include_all_versions: bool = False) -> tuple[ToolDescriptor, ...]:
        """Return schemas and metadata without exposing implementation callables."""
        with self._lock:
            definitions = [
                definition
                for versions in self._tools.values()
                for definition in (
                    versions.values()
                    if include_all_versions
                    else (versions[max(versions, key=_version_key)],)
                )
            ]
        descriptors = [
            ToolDescriptor(
                metadata=definition.metadata,
                input_schema=definition.input_model.model_json_schema(),
                output_schema=definition.output_model.model_json_schema(),
            )
            for definition in definitions
        ]
        return tuple(
            sorted(
                descriptors,
                key=lambda item: (item.metadata.name, _version_key(item.metadata.version)),
            )
        )

    def invoke(
        self,
        name: str,
        inputs: Mapping[str, Any] | BaseModel,
        *,
        context: ToolExecutionContext | None = None,
        version: str | None = None,
    ) -> ToolOutput:
        """Validate, invoke, time-bound, and validate the output of a registered tool."""
        definition = self.get(name, version)
        metadata = definition.metadata
        execution_context = context or ToolExecutionContext()
        if metadata.requires_approval and not execution_context.approved:
            raise ToolApprovalRequired(metadata.name, metadata.version)
        validated_input = _validate_input(definition.input_model, inputs, metadata)
        execution_context.checkpoint(metadata.name)
        deadline = time.monotonic() + metadata.timeout_seconds
        if not self._capacity.acquire(timeout=metadata.timeout_seconds):
            raise ToolBusyError(metadata.name, metadata.version)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._capacity.release()
            raise ToolTimeoutError(metadata.name, metadata.version, metadata.timeout_seconds)
        child_context = execution_context.with_timeout(remaining)
        outcome: dict[str, Any] = {}
        completed = threading.Event()

        def run_handler() -> None:
            try:
                child_context.checkpoint(metadata.name)
                outcome["value"] = definition.handler(validated_input, child_context)
            except Exception as error:
                outcome["error"] = error
            finally:
                self._capacity.release()
                completed.set()

        worker = threading.Thread(
            target=run_handler,
            name=f"agentflow-tool-{metadata.name}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as error:
            self._capacity.release()
            raise ToolExecutionError(
                "Tool worker could not be started",
                tool_name=metadata.name,
                tool_version=metadata.version,
            ) from error

        remaining = max(0.0, deadline - time.monotonic())
        if not completed.wait(remaining):
            child_context.cancel()
            raise ToolTimeoutError(metadata.name, metadata.version, metadata.timeout_seconds)

        error = outcome.get("error")
        if isinstance(error, ToolError):
            if error.code == "timeout":
                raise ToolTimeoutError(metadata.name, metadata.version, metadata.timeout_seconds)
            raise error
        if error is not None:
            raise ToolExecutionError(
                "Tool implementation failed",
                tool_name=metadata.name,
                tool_version=metadata.version,
            ) from None
        return _validate_output(definition.output_model, outcome.get("value"), metadata)


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def _validate_input(
    model: type[ToolInput],
    value: Mapping[str, Any] | BaseModel,
    metadata: ToolMetadata,
) -> ToolInput:
    try:
        if isinstance(value, BaseModel):
            payload = value.model_dump_json()
        elif isinstance(value, Mapping):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        else:
            raise TypeError("input must be a mapping or Pydantic model")
        if len(payload.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("input exceeds the size limit")
        return model.model_validate_json(payload)
    except ValidationError as error:
        issues = tuple(
            {"loc": tuple(item["loc"]), "type": item["type"]}
            for item in error.errors(include_input=False)
        )
        raise ToolInputError(
            f"Input validation failed for tool {metadata.name!r}",
            tool_name=metadata.name,
            tool_version=metadata.version,
            issues=issues,
        ) from None
    except (TypeError, ValueError):
        raise ToolInputError(
            f"Input for tool {metadata.name!r} must be valid JSON within the size limit",
            tool_name=metadata.name,
            tool_version=metadata.version,
        ) from None


def _validate_output(model: type[ToolOutput], value: Any, metadata: ToolMetadata) -> ToolOutput:
    try:
        if isinstance(value, BaseModel):
            payload = value.model_dump_json()
        elif isinstance(value, Mapping):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        else:
            raise TypeError("tool handler must return a Pydantic model or mapping")
        if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("output exceeds the size limit")
        decoded = json.loads(payload)
        json.dumps(decoded, ensure_ascii=False, allow_nan=False)
        return model.model_validate_json(payload)
    except ValidationError:
        raise ToolOutputError(
            f"Tool {metadata.name!r} returned output that failed schema validation",
            tool_name=metadata.name,
            tool_version=metadata.version,
        ) from None
    except (TypeError, ValueError):
        raise ToolOutputError(
            f"Tool {metadata.name!r} returned output that is not bounded JSON",
            tool_name=metadata.name,
            tool_version=metadata.version,
        ) from None
