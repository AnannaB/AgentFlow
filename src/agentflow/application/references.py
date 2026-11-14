"""Resolve explicit same-run step-output references without expression evaluation."""

import re
from typing import Any

from agentflow.application.errors import InvalidOutputReferenceError

_REFERENCE_KEY = "$agentflow_ref"
_INDEX_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
_MISSING = object()


def resolve_step_arguments(
    value: Any,
    *,
    current_step_key: str,
    direct_dependencies: set[str],
    successful_outputs: dict[str, dict[str, Any]],
) -> Any:
    """Return a JSON-compatible copy with exact reference markers replaced by values."""
    if isinstance(value, dict):
        if _REFERENCE_KEY in value:
            if set(value) != {_REFERENCE_KEY}:
                raise InvalidOutputReferenceError("Output reference marker has unexpected fields")
            reference = value[_REFERENCE_KEY]
            if not isinstance(reference, dict) or set(reference) != {"step_key", "pointer"}:
                raise InvalidOutputReferenceError(
                    "Output reference must contain step_key and pointer"
                )
            step_key = reference["step_key"]
            pointer = reference["pointer"]
            if not isinstance(step_key, str) or not step_key:
                raise InvalidOutputReferenceError(
                    "Output reference step_key must be a non-empty string"
                )
            if step_key == current_step_key or step_key not in direct_dependencies:
                raise InvalidOutputReferenceError(
                    "Output references may target only a direct dependency of the current step"
                )
            if step_key not in successful_outputs:
                raise InvalidOutputReferenceError(
                    "Output reference source has no successfully completed output in this run"
                )
            if not isinstance(pointer, str):
                raise InvalidOutputReferenceError("Output reference pointer must be a string")
            return _resolve_pointer(successful_outputs[step_key], pointer)
        return {
            key: resolve_step_arguments(
                nested,
                current_step_key=current_step_key,
                direct_dependencies=direct_dependencies,
                successful_outputs=successful_outputs,
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_step_arguments(
                nested,
                current_step_key=current_step_key,
                direct_dependencies=direct_dependencies,
                successful_outputs=successful_outputs,
            )
            for nested in value
        ]
    return value


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise InvalidOutputReferenceError("JSON Pointer must be empty or start with '/'")

    current = document
    for encoded_token in pointer[1:].split("/"):
        token = _decode_token(encoded_token)
        if isinstance(current, dict):
            current = current.get(token, _MISSING)
        elif isinstance(current, list):
            if len(token) > 20 or not _INDEX_PATTERN.fullmatch(token):
                raise InvalidOutputReferenceError("JSON Pointer array index is invalid")
            index = int(token)
            current = current[index] if index < len(current) else _MISSING
        else:
            current = _MISSING
        if current is _MISSING:
            raise InvalidOutputReferenceError("JSON Pointer does not identify an output value")
    return current


def _decode_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in "01":
            raise InvalidOutputReferenceError("JSON Pointer contains an invalid '~' escape")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)
