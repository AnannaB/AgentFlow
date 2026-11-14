"""Tests for registration, validation, and controlled invocation."""

import threading
import time

import pytest
from pydantic import ValidationError

from agentflow.tools.base import ToolDefinition, ToolInput, ToolMetadata, ToolOutput, ToolRisk
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import (
    DuplicateToolError,
    ToolApprovalRequired,
    ToolBusyError,
    ToolExecutionError,
    ToolInputError,
    ToolOutputError,
    ToolTimeoutError,
    UnknownToolError,
)
from agentflow.tools.registry import ToolRegistry


class NumberInput(ToolInput):
    value: int


class NumberOutput(ToolOutput):
    value: int


def definition(handler, *, version="1.0.0", timeout=1, risk=ToolRisk.LOW, requires_approval=False):
    return ToolDefinition(
        metadata=ToolMetadata(
            name="number",
            version=version,
            description="Return a validated number",
            risk=risk,
            timeout_seconds=timeout,
            retryable=True,
            idempotent=True,
            requires_approval=requires_approval,
        ),
        input_model=NumberInput,
        output_model=NumberOutput,
        handler=handler,
    )


def test_register_discover_versions_and_duplicate_rejection():
    registry = ToolRegistry()
    registry.register(definition(lambda data, context: NumberOutput(value=data.value)))
    registry.register(
        definition(lambda data, context: NumberOutput(value=data.value), version="2.0.0")
    )
    assert registry.versions("number") == ("1.0.0", "2.0.0")
    assert registry.get("number").metadata.version == "2.0.0"
    assert len(registry.discover()) == 1
    assert len(registry.discover(include_all_versions=True)) == 2
    with pytest.raises(DuplicateToolError):
        registry.register(definition(lambda data, context: NumberOutput(value=data.value)))


def test_invoke_validates_input_and_output_and_rejects_unknown_tool():
    registry = ToolRegistry()
    registry.register(definition(lambda data, context: NumberOutput(value=data.value + 1)))
    assert registry.invoke("number", {"value": 2}).value == 3
    with pytest.raises(ToolInputError):
        registry.invoke("number", {"value": "2"})
    with pytest.raises(ToolInputError):
        registry.invoke("number", {"value": 1, "extra": True})
    with pytest.raises(ToolOutputError):
        registry.register(
            ToolDefinition(
                metadata=ToolMetadata(name="bad_output", version="1.0.0", description="bad output"),
                input_model=NumberInput,
                output_model=NumberOutput,
                handler=lambda data, context: {"unknown": object()},
            )
        )
        registry.invoke("bad_output", {"value": 1})
    with pytest.raises(UnknownToolError):
        registry.invoke("missing", {})


def test_handler_failures_are_typed_and_sanitized():
    registry = ToolRegistry()
    registry.register(definition(lambda data, context: 1 / 0))
    with pytest.raises(ToolExecutionError, match="Tool implementation failed") as error:
        registry.invoke("number", {"value": 1})
    assert error.value.code == "execution_failed"
    assert error.value.tool_name == "number"


def test_approval_gate_and_timeout():
    registry = ToolRegistry()
    registry.register(
        definition(
            lambda data, context: NumberOutput(value=1),
            risk=ToolRisk.HIGH,
            requires_approval=True,
        )
    )
    with pytest.raises(ToolApprovalRequired):
        registry.invoke("number", {"value": 1})
    assert (
        registry.invoke("number", {"value": 1}, context=ToolExecutionContext(approved=True)).value
        == 1
    )

    timeout_registry = ToolRegistry()
    timeout_registry.register(
        definition(lambda data, context: (time.sleep(0.2), NumberOutput(value=1))[1], timeout=0.02)
    )
    with pytest.raises(ToolTimeoutError):
        timeout_registry.invoke("number", {"value": 1})
    time.sleep(0.22)


def test_timed_out_sync_handler_retains_capacity_until_it_exits():
    started = threading.Event()
    release = threading.Event()
    registry = ToolRegistry(max_concurrent=1)
    calls = 0

    def slow_handler(data, context):
        nonlocal calls
        calls += 1
        started.set()
        release.wait()
        return NumberOutput(value=data.value)

    registry.register(definition(slow_handler, timeout=0.05))
    try:
        with pytest.raises(ToolTimeoutError) as timeout:
            registry.invoke("number", {"value": 1})
        assert timeout.value.code == "timeout"
        assert started.is_set()
        with pytest.raises(ToolBusyError):
            registry.invoke("number", {"value": 2})
    finally:
        release.set()
    assert calls == 1
    assert registry.invoke("number", {"value": 3}).value == 3


def test_cooperative_deadline_is_reported_as_timeout():
    registry = ToolRegistry()

    def checkpointing_handler(data, context):
        while True:
            context.checkpoint("number")
            time.sleep(0.002)

    registry.register(definition(checkpointing_handler, timeout=0.03))
    with pytest.raises(ToolTimeoutError) as error:
        registry.invoke("number", {"value": 1})
    assert error.value.code == "timeout"


def test_contracts_are_strict_and_version_selection_is_explicit():
    with pytest.raises(ValidationError):
        NumberInput.model_validate({"value": 1, "extra": 2})
    registry = ToolRegistry()
    registry.register(definition(lambda data, context: NumberOutput(value=data.value)))
    with pytest.raises(UnknownToolError):
        registry.invoke("number", {"value": 1}, version="9.0.0")
