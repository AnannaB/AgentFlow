"""Dashboard composition, upload safety, and local diagnostics coverage."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.error import URLError

import pytest

from agentflow.application.execution import WorkflowExecutionService
from agentflow.config import Settings
from agentflow.dashboard.controllers import create_workflow_run, execute_workflow, generate_plan
from agentflow.dashboard.diagnostics import OllamaDiagnosticError, list_local_models
from agentflow.dashboard.services import build_dashboard_services
from agentflow.dashboard.uploads import UploadValidationError, validate_and_store_csv
from agentflow.domain.models import ExecutionPlan, PlanStep
from agentflow.domain.states import PlannerKind, RunStatus
from agentflow.planning.models import PlanningResult


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_dashboard_service_wiring_initializes_local_backend(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "db" / "agentflow.sqlite3",
        workspace_path=tmp_path / "workspace",
    )

    services = build_dashboard_services(settings)

    assert services.database.path == settings.database_path
    assert settings.database_path.is_file()
    assert services.workspace.is_dir()
    assert services.registry.discover()
    assert services.repository.count_runs_by_status() == {}
    assert isinstance(services.execution, WorkflowExecutionService)


def test_upload_is_validated_and_confined_even_with_path_like_filename(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    uploaded = validate_and_store_csv("..\\outside.csv", b"name,value\nalpha,2\n", workspace)

    assert uploaded.relative_path.startswith("uploads/")
    assert uploaded.path.is_relative_to(workspace)
    assert uploaded.path.read_bytes() == b"name,value\nalpha,2\n"
    assert uploaded.frame.to_dict(orient="records") == [{"name": "alpha", "value": 2}]


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("dataset.txt", b"a,b\n1,2\n", ".csv extension"),
        ("dataset.csv", b"\xff", "valid UTF-8 CSV"),
        ("dataset.csv", b'a,b\n"unterminated,2\n', "valid UTF-8 CSV"),
        ("dataset.csv", b"a,b\n", "header and at least one data row"),
    ],
)
def test_upload_validation_reports_bad_files(
    tmp_path: Path, filename: str, content: bytes, message: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UploadValidationError, match=message):
        validate_and_store_csv(filename, content, workspace)


def test_upload_rejects_files_over_ten_mib(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(UploadValidationError, match="10 MiB"):
        validate_and_store_csv("dataset.csv", b"x" * (10 * 1024 * 1024 + 1), workspace)


def test_ollama_connection_diagnostic_lists_local_models() -> None:
    settings = Settings(ollama_model="llama3.1")
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Response(b'{"models":[{"name":"llama3.1:latest"},{"name":"qwen2.5"}]}')

    assert list_local_models(settings, opener=opener) == ("llama3.1:latest", "qwen2.5")
    assert seen == {"url": "http://localhost:11434/api/tags", "timeout": 60.0}


def test_ollama_connection_diagnostic_returns_safe_error() -> None:
    settings = Settings()

    def opener(_request, timeout):
        raise URLError("connection refused")

    with pytest.raises(OllamaDiagnosticError, match="Could not connect to Ollama"):
        list_local_models(settings, opener=opener)


def test_ollama_connection_diagnostic_handles_timeout() -> None:
    settings = Settings()

    def opener(_request, timeout):
        raise TimeoutError

    with pytest.raises(OllamaDiagnosticError, match="timed out"):
        list_local_models(settings, opener=opener)


class _Planner:
    def __init__(self, result: PlanningResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def plan(self, objective: str) -> PlanningResult:
        self.calls.append(f"plan:{objective}")
        return self.result

    def create_run(self, result: PlanningResult, execution) -> object:
        self.calls.append("create_run")
        return execution.create_run(result.plan, objective=result.objective)


def test_planning_and_execution_are_separate_dashboard_actions(tmp_path: Path) -> None:
    services = build_dashboard_services(
        Settings(
            database_path=tmp_path / "agentflow.sqlite3",
            workspace_path=tmp_path / "workspace",
        )
    )
    plan = ExecutionPlan(
        objective_summary="Calculate a result",
        steps=[
            PlanStep(
                step_key="calculate",
                tool_name="calculator",
                purpose="Calculate a fixed expression",
                arguments={"expression": "6 * 7"},
            )
        ],
    )
    planning_result = PlanningResult(
        objective="Calculate 6 * 7",
        plan=plan,
        planner_kind=PlannerKind.RULE_BASED,
        fallback_reason="test rule",
        duration_ms=0,
    )
    planner = _Planner(planning_result)

    planned = generate_plan(planner, "Calculate 6 * 7")
    assert planned is planning_result
    assert services.repository.list_runs() == []

    run = create_workflow_run(planner, planned, services.execution)
    assert services.repository.get_run(run.id).status == RunStatus.QUEUED
    assert planner.calls == ["plan:Calculate 6 * 7", "create_run"]

    result = execute_workflow(services.execution, run.id, services.workspace)
    assert result.status == RunStatus.SUCCEEDED
    assert services.repository.get_run(run.id).status == RunStatus.SUCCEEDED
    assert planner.calls == ["plan:Calculate 6 * 7", "create_run"]


def test_invalid_ollama_response_is_not_exposed_as_sensitive_details() -> None:
    settings = Settings()

    def opener(_request, timeout):
        return _Response(b"not-json")

    with pytest.raises(OllamaDiagnosticError, match="invalid model list"):
        list_local_models(settings, opener=opener)
