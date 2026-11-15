"""Sanitized workflow reports and workspace-confined Markdown/JSON exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, ValidationError, model_validator

from agentflow.application.approvals import ApprovalService
from agentflow.application.errors import (
    ReportExportError,
    ReportNotFoundError,
    ReportNotReadyError,
    ReportTooLargeError,
)
from agentflow.domain.models import (
    DomainModel,
    StepAttempt,
    WorkflowRun,
    WorkflowStep,
)
from agentflow.domain.states import AttemptStatus, PlannerKind, RunStatus, StepStatus
from agentflow.storage.protocols import WorkflowRepository
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolError
from agentflow.tools.registry import MAX_INPUT_BYTES, ToolRegistry
from agentflow.tools.security import resolve_workspace_path

MAX_REPORT_BYTES = 48 * 1024
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|"
    r"authorization|credential)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


class ReportAttempt(DomainModel):
    """Redacted summary of one persisted tool invocation."""

    attempt_number: int = Field(ge=1)
    status: AttemptStatus
    duration_ms: int = Field(ge=0)
    output: dict[str, JsonValue] | None = None
    failure_summary: str | None = Field(default=None, max_length=1000)


class ReportStep(DomainModel):
    """Status, tool and attempts for one workflow step."""

    step_key: str = Field(min_length=1, max_length=100)
    tool_name: str = Field(min_length=1, max_length=100)
    status: StepStatus
    required: bool
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    attempts: list[ReportAttempt] = Field(default_factory=list, max_length=500)
    failure_summary: str | None = Field(default=None, max_length=1000)


class WorkflowReport(DomainModel):
    """Structured report for a succeeded or failed workflow run."""

    run_id: UUID
    objective: str = Field(min_length=1, max_length=4000)
    planner_kind: PlannerKind
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = Field(ge=0)
    status: RunStatus
    steps: list[ReportStep] = Field(max_length=100)
    failure_summary: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def report_is_bounded(self) -> WorkflowReport:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_REPORT_BYTES:
            raise ValueError(f"workflow report exceeds {MAX_REPORT_BYTES}-byte limit")
        return self


class ReportExportRequest(DomainModel):
    """Persisted approval scope for a specific report destination and content."""

    run_id: UUID
    approval_id: UUID
    output_format: Literal["json"] = "json"
    relative_path: str = Field(min_length=1, max_length=4096)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(min_length=1, max_length=20)


class ReportExportResult(DomainModel):
    """Workspace-relative path and checksum for an exported report."""

    run_id: UUID
    output_format: Literal["json", "markdown"]
    relative_path: str = Field(min_length=1, max_length=4096)
    bytes_written: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_id: UUID | None = None


class WorkflowReportService:
    """Build safe reports and export through registered report tools."""

    def __init__(
        self,
        repository: WorkflowRepository,
        registry: ToolRegistry,
        approvals: ApprovalService,
        *,
        workspace_roots: tuple[str | Path, ...],
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._approvals = approvals
        self._context = ToolExecutionContext(workspace_roots=workspace_roots)

    def build_report(self, run_id: UUID) -> WorkflowReport:
        """Build a bounded report from persisted run, steps and attempts."""
        run = self._repository.get_run(run_id)
        if run is None:
            raise ReportNotFoundError("Workflow run was not found")
        if run.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            raise ReportNotReadyError("A report is available after a run succeeds or fails")

        steps = self._repository.list_steps(run_id)
        report_steps = [self._step_report(step) for step in steps]
        duration_ms = self._run_duration(run, report_steps)
        try:
            return WorkflowReport(
                run_id=run.id,
                objective=_redact_text(run.objective),
                planner_kind=run.planner_kind,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                duration_ms=duration_ms,
                status=run.status,
                steps=report_steps,
                failure_summary=_redact_text(run.error_summary) if run.error_summary else None,
            )
        except ValidationError as error:
            if any("workflow report exceeds" in issue["msg"] for issue in error.errors()):
                raise ReportTooLargeError("Workflow report exceeds the 48 KiB limit") from None
            raise ReportExportError(
                "Persisted workflow data could not be validated for reporting"
            ) from None

    def request_json_export(self, run_id: UUID, output_path: str) -> ReportExportRequest:
        """Create a persisted approval request for an exact report and output path."""
        report = self.build_report(run_id)
        destination = self._resolve_output(output_path, ".json")
        relative_path = self._relative_path(destination)
        digest = _export_digest(report, relative_path)
        approval = self._approvals.request_report_export(run_id, digest)
        return ReportExportRequest(
            run_id=run_id,
            approval_id=approval.id,
            relative_path=relative_path,
            digest=digest,
            status=approval.status.value,
        )

    def export_json(
        self,
        run_id: UUID,
        output_path: str,
        *,
        approval_id: UUID,
    ) -> ReportExportResult:
        """Export JSON only after verifying persisted approval for exact content/path."""
        report = self.build_report(run_id)
        destination = self._resolve_output(output_path, ".json")
        relative_path = self._relative_path(destination)
        digest = _export_digest(report, relative_path)
        self._approvals.verify_report_export(approval_id, run_id=run_id, digest=digest)
        tool_input = {"output_path": str(destination), "payload": report.model_dump(mode="json")}
        _check_tool_input_size(tool_input)
        try:
            # The registry approval context is created only after persisted approval verifies.
            approved_context = self._context.with_approval()
            result = self._registry.invoke(
                "json_exporter",
                tool_input,
                context=approved_context,
            )
        except ToolError:
            raise ReportExportError("Registered JSON report export failed") from None
        return ReportExportResult(
            run_id=run_id,
            output_format="json",
            relative_path=result.relative_path,
            bytes_written=result.bytes_written,
            sha256=result.sha256,
            approval_id=approval_id,
        )

    def export_markdown(self, run_id: UUID, output_path: str) -> ReportExportResult:
        """Generate Markdown with the registered tool and atomically save in workspace."""
        report = self.build_report(run_id)
        destination = self._resolve_output(output_path, ".md", ".markdown")
        input_data = _markdown_input(report)
        _check_tool_input_size(input_data)
        try:
            generated = self._registry.invoke(
                "markdown_report_generator", input_data, context=self._context
            )
        except ToolError:
            raise ReportExportError("Registered Markdown report generation failed") from None
        content = generated.markdown.encode("utf-8")
        if len(content) > 64 * 1024:
            raise ReportTooLargeError("Markdown report exceeds the 64 KiB limit")
        written = _atomic_write(destination, content, self._context)
        return ReportExportResult(
            run_id=run_id,
            output_format="markdown",
            relative_path=self._relative_path(destination),
            bytes_written=written,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def _step_report(self, step: WorkflowStep) -> ReportStep:
        attempts = self._repository.list_attempts(step.id)
        reports = [self._attempt_report(attempt) for attempt in attempts]
        latest_failure = (
            next((item.error_summary for item in reversed(attempts) if item.error_summary), None)
            if step.status == StepStatus.FAILED
            else None
        )
        return ReportStep(
            step_key=step.step_key,
            tool_name=step.tool_name,
            status=step.status,
            required=step.required,
            depends_on=list(step.depends_on),
            attempts=reports,
            failure_summary=_redact_text(latest_failure) if latest_failure else None,
        )

    def _attempt_report(self, attempt: StepAttempt) -> ReportAttempt:
        output = attempt.output.data if attempt.output and attempt.output.success else None
        return ReportAttempt(
            attempt_number=attempt.attempt_number,
            status=attempt.status,
            duration_ms=attempt.duration_ms or 0,
            output=_redact_value(output) if output is not None else None,
            failure_summary=(
                _redact_text(attempt.error_summary) if attempt.error_summary else None
            ),
        )

    def _resolve_output(self, output_path: str, *suffixes: str) -> Path:
        try:
            destination = resolve_workspace_path(
                output_path,
                self._context,
                must_exist=False,
                tool_name="workflow_report_export",
            )
        except ToolError:
            raise ReportExportError("Report path must stay within a configured workspace") from None
        if destination.suffix.lower() not in suffixes:
            raise ReportExportError(f"Report path must end with one of: {', '.join(suffixes)}")
        return destination

    def _relative_path(self, destination: Path) -> str:
        for root in self._context.workspace_roots:
            try:
                return destination.relative_to(root).as_posix()
            except ValueError:
                continue
        raise ReportExportError("Report destination is outside configured workspace roots")

    @staticmethod
    def _run_duration(run: WorkflowRun, steps: list[ReportStep]) -> int:
        if run.started_at is not None and run.finished_at is not None:
            return max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))
        return sum(attempt.duration_ms for step in steps for attempt in step.attempts)


def _markdown_input(report: WorkflowReport) -> dict[str, object]:
    summary_parts = [
        f"Run {report.run_id} finished with status {report.status.value}.",
        f"Objective: {report.objective}",
        f"Planner: {report.planner_kind}",
        f"Duration: {report.duration_ms} ms",
    ]
    if report.failure_summary:
        summary_parts.append(f"Failure: {report.failure_summary}")
    findings: list[dict[str, str]] = []
    for step in report.steps:
        detail = (
            f"Status: {step.status.value}; required: {step.required}; "
            f"dependencies: {', '.join(step.depends_on) or 'none'}; attempts: {len(step.attempts)}."
        )
        if step.failure_summary:
            detail += f" Failure: {step.failure_summary}"
        for attempt in step.attempts:
            if attempt.output is not None:
                encoded = json.dumps(
                    attempt.output, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                detail += f" Attempt {attempt.attempt_number} output: {encoded[:1000]}"
        findings.append(
            {
                "title": f"{step.step_key} — {step.tool_name} — {step.status.value}",
                "description": detail[:2000] or "No additional details.",
            }
        )
    return {
        "title": f"Workflow report {report.run_id}",
        "summary": "\n".join(summary_parts)[:4000],
        "findings": findings,
    }


def _redact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(key) else _redact_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _API_TOKEN.sub("[REDACTED]", value)


def _export_digest(report: WorkflowReport, relative_path: str) -> str:
    payload = {
        "format": "json",
        "run_id": str(report.run_id),
        "relative_path": relative_path,
        "report": report.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_tool_input_size(value: dict[str, object]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_INPUT_BYTES:
        raise ReportTooLargeError("Report exceeds the registered tool input size limit")


def _atomic_write(destination: Path, content: bytes, context: ToolExecutionContext) -> int:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ReportExportError("Report output directory could not be created") from None
    destination = resolve_workspace_path(
        str(destination), context, must_exist=False, tool_name="workflow_report_export"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".agentflow-report-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError:
        raise ReportExportError("Report could not be written in the configured workspace") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return len(content)
