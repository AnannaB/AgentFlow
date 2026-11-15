"""Deterministic sales demo plan and execution coverage."""

import json
from pathlib import Path

from agentflow.config import Settings
from agentflow.dashboard.sales_demo import create_sales_demo_plan, supports_sales_demo
from agentflow.dashboard.services import build_dashboard_services
from agentflow.dashboard.uploads import validate_and_store_csv
from agentflow.domain.states import RunStatus


def _uploaded_sales(workspace: Path):
    workspace.mkdir(parents=True)
    content = (
        b"order_id,date,product,category,quantity,unit_price\n"
        b"1,2026-01-05,Laptop,Electronics,2,10\n"
        b"2,2026-01-08,Mouse,Accessories,5,2\n"
        b"3,2026-02-03,Laptop,Electronics,1,10\n"
    )
    return validate_and_store_csv("sales_demo.csv", content, workspace)


def test_sales_demo_creates_valid_plan_and_runs_registered_tools(tmp_path: Path):
    workspace = tmp_path / "workspace"
    uploaded = _uploaded_sales(workspace)
    settings = Settings(
        database_path=tmp_path / "agentflow.sqlite3",
        workspace_path=workspace,
    )
    services = build_dashboard_services(settings)

    result, prepared = create_sales_demo_plan(uploaded, str(workspace), services.registry)

    assert result.planner_kind.value == "rule_based"
    assert result.fallback_reason == "deterministic_sales_demo"
    assert uploaded.path != prepared.path
    assert {"revenue", "month"}.issubset(prepared.frame.columns)
    assert [step.tool_name for step in result.plan.steps] == [
        "csv_profiler",
        "data_aggregator",
        "data_aggregator",
    ]
    run = services.execution.create_run(
        result.plan,
        objective=result.objective,
        planner_kind=result.planner_kind,
    )
    execution = services.execution.execute(run.id, workspace_roots=(str(workspace),))

    assert execution.status == RunStatus.SUCCEEDED
    product_output = next(
        step.output for step in execution.steps if step.step_key == "revenue_by_product"
    )
    month_output = next(
        step.output for step in execution.steps if step.step_key == "revenue_by_month"
    )
    assert product_output["rows"] == [
        {"product": "Laptop", "revenue": 30},
        {"product": "Mouse", "revenue": 10},
    ]
    assert month_output["rows"] == [
        {"month": "2026-01", "revenue": 30},
        {"month": "2026-02", "revenue": 10},
    ]
    assert max(product_output["rows"], key=lambda row: row["revenue"])["product"] == "Laptop"

    markdown = services.reports.export_markdown(run.id, f"reports/{run.id}.md")
    request = services.reports.request_json_export(run.id, f"reports/{run.id}.json")
    services.approvals.approve(
        request.approval_id,
        run_id=run.id,
        step_id=None,
        plan_digest=request.digest,
        decision_note="Approve the local demo report export.",
    )
    json_export = services.reports.export_json(
        run.id,
        f"reports/{run.id}.json",
        approval_id=request.approval_id,
    )

    assert markdown.bytes_written > 0
    assert json_export.bytes_written > 0
    exported_report = json.loads((workspace / json_export.relative_path).read_text("utf-8"))
    assert exported_report["status"] == "succeeded"
    assert (workspace / markdown.relative_path).is_file()


def test_sales_demo_rejects_dataset_missing_required_fields(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    uploaded = validate_and_store_csv("other.csv", b"name,value\na,1\n", workspace)

    assert not supports_sales_demo(uploaded)
