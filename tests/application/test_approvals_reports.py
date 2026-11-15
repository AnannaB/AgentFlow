"""Approval decision and workflow report integration coverage."""

import json
from pathlib import Path

import pytest

from agentflow.application.approvals import ApprovalService
from agentflow.application.errors import (
    ApprovalScopeError,
    ApprovalStateError,
    ReportExportError,
    ReportNotReadyError,
    ReportTooLargeError,
)
from agentflow.application.execution import WorkflowExecutionService
from agentflow.application.reports import WorkflowReportService
from agentflow.domain.models import ExecutionPlan, PlanStep
from agentflow.domain.states import ApprovalStatus, PlannerKind, RunStatus, StepStatus
from agentflow.storage.sqlite import SQLiteDatabase, SQLiteRepository
from agentflow.tools.base import ToolDefinition, ToolInput, ToolMetadata, ToolOutput, ToolRisk
from agentflow.tools.builtins import create_builtin_registry


class TaskInput(ToolInput):
    value: str


class TaskOutput(ToolOutput):
    message: str
    api_token: str
    authorization: str


class FailedInput(ToolInput):
    value: str


class FailedOutput(ToolOutput):
    value: str


class LargeOutput(ToolOutput):
    payload: str


@pytest.fixture
def services(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "reporting.sqlite3")
    database.initialize()
    repository = SQLiteRepository(database)
    registry = create_builtin_registry()
    invocation_log: list[str] = []
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name="sensitive_task",
                version="1.0.0",
                description="Returns data requiring approval.",
                risk=ToolRisk.HIGH,
                requires_approval=True,
            ),
            input_model=TaskInput,
            output_model=TaskOutput,
            handler=lambda data, context: (
                invocation_log.append(data.value)
                or TaskOutput(
                    message=f"Completed {data.value}",
                    api_token="token-secret-value",
                    authorization="Bearer private-token-value",
                )
            ),
        )
    )
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name="large_output",
                version="1.0.0",
                description="Returns a payload large enough to exceed report limits.",
            ),
            input_model=FailedInput,
            output_model=LargeOutput,
            handler=lambda data, context: LargeOutput(payload="x" * 50_000),
        )
    )
    registry.register(
        ToolDefinition(
            metadata=ToolMetadata(
                name="known_failure",
                version="1.0.0",
                description="Fails deterministically for report coverage.",
            ),
            input_model=FailedInput,
            output_model=FailedOutput,
            handler=lambda data, context: (_ for _ in ()).throw(ValueError("private failure")),
        )
    )
    execution = WorkflowExecutionService(repository, registry)
    approvals = ApprovalService(repository, execution)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report_service = WorkflowReportService(
        repository,
        registry,
        approvals,
        workspace_roots=(workspace,),
    )
    return repository, registry, execution, approvals, report_service, invocation_log


def _plan(tool_name: str, *, required=True, value="payload"):
    return ExecutionPlan(
        objective_summary="Run the reporting test objective",
        steps=[
            PlanStep(
                step_key="operation",
                tool_name=tool_name,
                purpose="Run one registered test operation",
                arguments={"value": value},
                required=required,
                risk_level="high" if tool_name == "sensitive_task" else "low",
            )
        ],
    )


def _approve_request(approval_service, request):
    return approval_service.approve(
        request.id,
        run_id=request.run_id,
        step_id=request.step_id,
        plan_digest=request.plan_digest,
    )


def test_pending_approval_blocks_tool_until_exact_request_is_approved(services):
    repository, _, execution, approvals, _, invocations = services
    run = execution.create_run(_plan("sensitive_task"))

    waiting = execution.execute(run.id)

    assert waiting.status == RunStatus.AWAITING_APPROVAL
    assert invocations == []
    request = approvals.list_pending(run_id=run.id)[0]
    digest = execution.approval_digest(run.id, request.step_id)
    assert request.plan_digest == digest

    with pytest.raises(ApprovalScopeError):
        approvals.approve(
            request.id,
            run_id=run.id,
            step_id=None,
            plan_digest=digest,
        )
    with pytest.raises(ApprovalScopeError):
        approvals.approve(
            request.id,
            run_id=run.id,
            step_id=request.step_id,
            plan_digest="0" * 64,
        )
    assert invocations == []

    resolved = _approve_request(approvals, request)
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.resolved_at is not None
    assert any(event.event_type == "approval.approved" for event in repository.list_events(run.id))

    completed = execution.execute(run.id)

    assert completed.status == RunStatus.SUCCEEDED
    assert invocations == ["payload"]


def test_rejected_approval_never_invokes_protected_tool(services):
    _, _, execution, approvals, _, invocations = services
    run = execution.create_run(_plan("sensitive_task"))
    execution.execute(run.id)
    request = approvals.list_pending(run_id=run.id)[0]

    rejected = approvals.reject(
        request.id,
        run_id=run.id,
        step_id=request.step_id,
        plan_digest=request.plan_digest,
        decision_note="Declined for this run",
    )
    result = execution.execute(run.id)

    assert rejected.status == ApprovalStatus.REJECTED
    assert result.status == RunStatus.FAILED
    assert invocations == []


def test_old_approval_cannot_override_new_pending_request(services):
    repository, _, execution, approvals, _, invocations = services
    run = execution.create_run(_plan("sensitive_task"))
    execution.execute(run.id)
    first = approvals.list_pending(run_id=run.id)[0]
    _approve_request(approvals, first)
    second = repository.create_approval(
        type(first)(run_id=run.id, step_id=first.step_id, plan_digest=first.plan_digest)
    )

    assert execution.approval_decision(run.id, second.step_id, second.plan_digest) == "pending"
    result = execution.execute(run.id)
    assert result.status == RunStatus.AWAITING_APPROVAL
    assert invocations == []

    approvals.reject(
        second.id,
        run_id=run.id,
        step_id=second.step_id,
        plan_digest=second.plan_digest,
    )
    rejected = execution.execute(run.id)
    assert rejected.status == RunStatus.FAILED
    assert invocations == []


def test_workflow_report_includes_provenance_outputs_and_redacts_secrets(services):
    _, _, execution, approvals, reports, _ = services
    run = execution.create_run(
        _plan("sensitive_task", value="password=hunter2"),
        objective="password=hunter2",
        planner_kind=PlannerKind.OLLAMA,
    )
    execution.execute(run.id)
    request = approvals.list_pending(run_id=run.id)[0]
    _approve_request(approvals, request)
    execution.execute(run.id)

    report = reports.build_report(run.id)
    output = report.steps[0].attempts[0].output

    assert report.status == RunStatus.SUCCEEDED
    assert report.planner_kind == PlannerKind.OLLAMA
    assert report.duration_ms >= 0
    assert report.steps[0].status == StepStatus.SUCCEEDED
    assert report.steps[0].attempts[0].status.value == "succeeded"
    assert output is not None
    assert output["api_token"] == "[REDACTED]"
    assert output["authorization"] == "[REDACTED]"
    assert "password=[REDACTED]" in report.objective
    assert "hunter2" not in report.model_dump_json()
    assert "token-secret-value" not in report.model_dump_json()


def test_workflow_report_handles_failed_run_and_optional_step(services):
    _, _, execution, _, reports, _ = services
    failed_run = execution.create_run(_plan("known_failure"))
    failed = execution.execute(failed_run.id)
    assert failed.status == RunStatus.FAILED
    failed_report = reports.build_report(failed_run.id)
    assert failed_report.steps[0].status == StepStatus.FAILED
    assert failed_report.failure_summary
    assert "private failure" not in failed_report.model_dump_json()

    optional_run = execution.create_run(_plan("known_failure", required=False))
    optional = execution.execute(optional_run.id)
    optional_report = reports.build_report(optional_run.id)
    assert optional.status == RunStatus.SUCCEEDED
    assert optional_report.steps[0].required is False
    assert optional_report.steps[0].status == StepStatus.FAILED
    assert optional_report.steps[0].failure_summary


def test_report_requires_terminal_run(services):
    _, _, execution, _, reports, _ = services
    run = execution.create_run(_plan("known_failure"))
    with pytest.raises(ReportNotReadyError):
        reports.build_report(run.id)


def test_markdown_export_uses_registered_generator_and_workspace_path(services, tmp_path):
    _, _, execution, approvals, reports, _ = services
    run = execution.create_run(_plan("sensitive_task", value="password=hunter2"))
    execution.execute(run.id)
    _approve_request(approvals, approvals.list_pending(run_id=run.id)[0])
    execution.execute(run.id)

    exported = reports.export_markdown(run.id, "reports/summary.md")
    report_path = tmp_path / "workspace" / "reports" / "summary.md"
    content = report_path.read_text(encoding="utf-8")

    assert exported.relative_path == "reports/summary.md"
    assert exported.bytes_written == len(content.encode("utf-8"))
    assert exported.sha256
    assert "Workflow report" in content
    assert "REDACTED" in content
    assert "hunter2" not in content


def test_json_export_requires_exact_persisted_export_approval(services, tmp_path):
    repository, _, execution, approvals, reports, _ = services
    run = execution.create_run(_plan("known_failure"))
    execution.execute(run.id)
    requested = reports.request_json_export(run.id, "reports/workflow.json")

    assert requested.status == "pending"
    assert requested.approval_id in {item.id for item in approvals.list_pending()}
    with pytest.raises(ApprovalStateError):
        reports.export_json(run.id, "reports/workflow.json", approval_id=requested.approval_id)

    approvals.approve(
        requested.approval_id,
        run_id=run.id,
        step_id=None,
        plan_digest=requested.digest,
    )
    with pytest.raises(ApprovalScopeError):
        reports.export_json(run.id, "reports/other.json", approval_id=requested.approval_id)
    exported = reports.export_json(
        run.id, "reports/workflow.json", approval_id=requested.approval_id
    )
    stored = json.loads((tmp_path / "workspace" / "reports" / "workflow.json").read_text())

    assert exported.relative_path == "reports/workflow.json"
    assert exported.approval_id == requested.approval_id
    assert stored["run_id"] == str(run.id)
    assert "private failure" not in json.dumps(stored)
    assert any(event.event_type == "approval.approved" for event in repository.list_events(run.id))


def test_report_export_rejects_path_traversal(services):
    _, _, execution, _, reports, _ = services
    run = execution.create_run(_plan("known_failure"))
    execution.execute(run.id)
    with pytest.raises(ReportExportError, match="workspace"):
        reports.export_markdown(run.id, "../outside.md")


def test_report_rejects_structured_output_over_size_limit(services):
    _, _, execution, _, reports, _ = services
    run = execution.create_run(_plan("large_output"))
    result = execution.execute(run.id)

    assert result.status == RunStatus.SUCCEEDED
    with pytest.raises(ReportTooLargeError, match="48 KiB"):
        reports.build_report(run.id)
