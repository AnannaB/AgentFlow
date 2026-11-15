"""Deterministic planning, validation and explicit execution coverage."""

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from agentflow.application.execution import WorkflowExecutionService
from agentflow.config import Settings
from agentflow.domain.models import ExecutionPlan
from agentflow.domain.states import PlannerKind, RunStatus
from agentflow.planning.errors import (
    FallbackUnavailableError,
    OllamaConnectionError,
    OllamaResponseError,
    PlanValidationError,
)
from agentflow.planning.fallback import RuleBasedPlanner
from agentflow.planning.ollama import MAX_OLLAMA_BODY_BYTES, OllamaClient
from agentflow.planning.service import PlanningService
from agentflow.planning.validation import PlanValidator
from agentflow.storage.sqlite import SQLiteDatabase, SQLiteRepository
from agentflow.tools.base import ToolDefinition, ToolInput, ToolMetadata, ToolOutput, ToolRisk
from agentflow.tools.builtins.calculator import calculator_definition
from agentflow.tools.registry import ToolRegistry


class EchoInput(ToolInput):
    value: str


class EchoOutput(ToolOutput):
    value: str


def _registry(handler=None):
    registry = ToolRegistry()
    registry.register(calculator_definition())
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name="echo_text",
                version="1.0.0",
                description="Return the supplied text.",
            ),
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=handler or (lambda data, context: EchoOutput(value=data.value)),
        )
    )
    return registry


def _plan_json(tool="echo_text", arguments=None, **updates):
    plan = {
        "objective_summary": "Echo a short value",
        "steps": [
            {
                "step_key": "echo",
                "tool_name": tool,
                "purpose": "Return the requested text",
                "arguments": arguments if arguments is not None else {"value": "hello"},
                "depends_on": [],
                "required": True,
                "risk_level": "low",
            }
        ],
        "completion_criteria": ["The value is returned"],
        "requires_approval": False,
    }
    plan.update(updates)
    return json.dumps(plan)


class StubClient:
    model = "test-local-model"

    def __init__(self, response=None, error=None):
        self.response = response if response is not None else _plan_json()
        self.error = error
        self.calls = []

    def generate_plan(self, objective, tools):
        self.calls.append((objective, tools))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def settings():
    return Settings(
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="test-model",
        ollama_timeout_seconds=1.5,
        ollama_fallback_enabled=True,
        _env_file=None,
    )


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def test_ollama_client_posts_local_chat_request_with_schema_and_timeout(settings):
    captured = {}
    body = json.dumps({"message": {"content": _plan_json()}}).encode()

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeHTTPResponse(body)

    client = OllamaClient(settings, opener=opener)
    response = client.generate_plan("Echo hello", _registry().discover())

    assert json.loads(response)["steps"][0]["tool_name"] == "echo_text"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["timeout"] == 1.5
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["format"]["title"] == "ExecutionPlan"
    tool_catalog = json.loads(captured["payload"]["messages"][1]["content"])["registered_tools"]
    assert "input_schema" in tool_catalog[0]
    assert "output_schema" in tool_catalog[0]


def test_ollama_client_maps_connection_and_timeout_errors(settings):
    def disconnected(*args, **kwargs):
        raise URLError("private host details")

    with pytest.raises(OllamaConnectionError, match="Could not connect"):
        OllamaClient(settings, opener=disconnected).generate_plan("objective", ())

    def timed_out(*args, **kwargs):
        raise TimeoutError

    with pytest.raises(OllamaConnectionError, match="timed out"):
        OllamaClient(settings, opener=timed_out).generate_plan("objective", ())


@pytest.mark.parametrize(
    "body",
    [b"not json", b"{}", b'{"message":{"content":""}}'],
)
def test_ollama_client_rejects_malformed_responses(settings, body):
    client = OllamaClient(settings, opener=lambda *args, **kwargs: FakeHTTPResponse(body))
    with pytest.raises(OllamaResponseError):
        client.generate_plan("objective", ())


def test_ollama_client_bounds_response_size(settings):
    body = b"x" * (MAX_OLLAMA_BODY_BYTES + 1)
    client = OllamaClient(settings, opener=lambda *args, **kwargs: FakeHTTPResponse(body))
    with pytest.raises(OllamaResponseError, match="1 MiB"):
        client.generate_plan("objective", ())


def test_plan_validator_rejects_unregistered_tools_and_bad_arguments():
    validator = PlanValidator(_registry())
    with pytest.raises(PlanValidationError, match="unregistered tool"):
        validator.validate(_plan_json(tool="run_shell"))
    with pytest.raises(PlanValidationError, match="input schema"):
        validator.validate(_plan_json(arguments={"value": 42}))


def test_plan_validator_rejects_invalid_dependency_graph():
    plan = json.loads(_plan_json())
    plan["steps"][0]["depends_on"] = ["missing"]
    with pytest.raises(PlanValidationError, match="unknown dependencies"):
        PlanValidator(_registry()).validate(plan)


def test_plan_validator_raises_risk_to_registered_tool_floor():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name="protected_tool",
                version="1.0.0",
                description="High impact operation",
                risk=ToolRisk.HIGH,
                requires_approval=True,
            ),
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=lambda data, context: EchoOutput(value=data.value),
        )
    )
    plan = json.loads(_plan_json(tool="protected_tool"))
    plan["steps"][0]["risk_level"] = "low"

    validated = PlanValidator(registry).validate(plan)

    assert validated.steps[0].risk_level == "high"


def test_plan_validator_shows_approval_requirement_as_at_least_medium_risk():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name="protected_tool",
                version="1.0.0",
                description="Requires approval",
                requires_approval=True,
            ),
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=lambda data, context: EchoOutput(value=data.value),
        )
    )
    plan = json.loads(_plan_json(tool="protected_tool"))

    validated = PlanValidator(registry).validate(plan)

    assert validated.steps[0].risk_level == "medium"


def test_planning_service_returns_ollama_provenance_without_executing(settings):
    calls = []
    registry = _registry(
        lambda data, context: calls.append(data.value) or EchoOutput(value=data.value)
    )
    client = StubClient()
    service = PlanningService(registry, settings, client=client)

    result = service.plan("Echo hello")

    assert result.planner_kind == PlannerKind.OLLAMA
    assert result.model == "test-local-model"
    assert result.fallback_reason is None
    assert result.plan.steps[0].tool_name == "echo_text"
    assert calls == []
    assert client.calls[0][0] == "Echo hello"


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        ("not json", "invalid_ollama_plan"),
        (_plan_json(tool="not_registered"), "invalid_ollama_plan"),
    ],
)
def test_invalid_ollama_plan_uses_explicit_rule_fallback(settings, response, expected_reason):
    service = PlanningService(_registry(), settings, client=StubClient(response=response))

    result = service.plan("Calculate 6 * 7")

    assert result.planner_kind == PlannerKind.RULE_BASED
    assert result.fallback_reason == expected_reason
    assert result.plan.steps[0].tool_name == "calculator"
    assert result.plan.steps[0].arguments == {"expression": "6 * 7"}


def test_connection_failure_uses_fallback_and_discloses_reason(settings, caplog):
    service = PlanningService(
        _registry(), settings, client=StubClient(error=OllamaConnectionError("offline"))
    )

    result = service.plan("Calculate 6 * 7")

    assert result.planner_kind == PlannerKind.RULE_BASED
    assert result.fallback_reason == "ollama_unavailable"
    assert "Calculate 6 * 7" not in caplog.text
    assert "offline" not in caplog.text


def test_disabled_fallback_propagates_ollama_error(settings):
    settings = settings.model_copy(update={"ollama_fallback_enabled": False})
    service = PlanningService(
        _registry(), settings, client=StubClient(error=OllamaConnectionError("offline"))
    )
    with pytest.raises(OllamaConnectionError):
        service.plan("Calculate 6 * 7")


def test_rule_fallback_only_handles_recognized_registered_intents():
    planner = RuleBasedPlanner(_registry())
    with pytest.raises(FallbackUnavailableError):
        planner.plan("Please analyze my files and send the result")
    with pytest.raises(FallbackUnavailableError, match="registered 'calculator'"):
        RuleBasedPlanner(ToolRegistry()).plan("Calculate 6 * 7")


def test_objective_validation_rejects_empty_and_oversized_inputs(settings):
    service = PlanningService(_registry(), settings, client=StubClient())
    with pytest.raises(PlanValidationError, match="must not be empty"):
        service.plan("  ")
    with pytest.raises(PlanValidationError, match="4000-character"):
        service.plan("x" * 4001)


def test_planner_to_execution_integration_uses_existing_engine_and_provenance(
    settings, tmp_path: Path
):
    executions = []
    registry = _registry(
        lambda data, context: executions.append(data.value) or EchoOutput(value=data.value)
    )
    planner = PlanningService(registry, settings, client=StubClient())
    database = SQLiteDatabase(tmp_path / "planner.sqlite3")
    database.initialize()
    repository = SQLiteRepository(database)
    executor = WorkflowExecutionService(repository, registry)

    planned = planner.plan("Echo hello")
    assert executions == []
    run = planner.create_run(planned, executor)
    assert repository.get_run(run.id).planner_kind == PlannerKind.OLLAMA
    assert repository.get_run(run.id).status == RunStatus.QUEUED
    assert repository.get_run(run.id).objective == "Echo hello"
    assert executions == []

    result = executor.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    assert executions == ["hello"]


def test_explicit_planning_service_execute_runs_only_when_called(settings, tmp_path: Path):
    executions = []
    registry = _registry(
        lambda data, context: executions.append(data.value) or EchoOutput(value=data.value)
    )
    planner = PlanningService(registry, settings, client=StubClient())
    database = SQLiteDatabase(tmp_path / "explicit.sqlite3")
    database.initialize()
    executor = WorkflowExecutionService(SQLiteRepository(database), registry)

    result = planner.plan("Echo hello")
    assert executions == []

    completed = planner.execute(result, executor)

    assert completed.status == RunStatus.SUCCEEDED
    assert executions == ["hello"]


def test_planner_accepts_existing_execution_plan_instance():
    plan = ExecutionPlan.model_validate_json(_plan_json())
    validated = PlanValidator(_registry()).validate(plan)
    assert validated == plan
