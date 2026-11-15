"""Composition root for dashboard access to existing application services."""

from dataclasses import dataclass
from pathlib import Path

from agentflow.application.approvals import ApprovalService
from agentflow.application.execution import WorkflowExecutionService
from agentflow.application.reports import WorkflowReportService
from agentflow.config import Settings, get_settings
from agentflow.storage.sqlite import SQLiteDatabase, SQLiteRepository
from agentflow.tools.builtins import create_builtin_registry
from agentflow.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class DashboardServices:
    """Shared backend services used by the Streamlit views."""

    database: SQLiteDatabase
    repository: SQLiteRepository
    registry: ToolRegistry
    execution: WorkflowExecutionService
    approvals: ApprovalService
    reports: WorkflowReportService
    workspace: Path


def build_dashboard_services(settings: Settings | None = None) -> DashboardServices:
    """Initialize local persistence and wire the existing AgentFlow services."""
    configured = settings or get_settings()
    workspace = configured.workspace_path.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(configured.database_path.expanduser())
    database.initialize()
    repository = SQLiteRepository(database)
    registry = create_builtin_registry()
    execution = WorkflowExecutionService(repository, registry)
    approvals = ApprovalService(repository, execution)
    reports = WorkflowReportService(repository, registry, approvals, workspace_roots=(workspace,))
    return DashboardServices(
        database=database,
        repository=repository,
        registry=registry,
        execution=execution,
        approvals=approvals,
        reports=reports,
        workspace=workspace,
    )
