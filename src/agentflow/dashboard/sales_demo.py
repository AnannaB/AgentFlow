"""Build a deterministic, registry-validated plan for the bundled sales CSV demo."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from agentflow.dashboard.uploads import UploadedDataset, validate_and_store_csv
from agentflow.domain.models import ExecutionPlan, PlanStep
from agentflow.domain.states import PlannerKind
from agentflow.planning.models import PlanningResult
from agentflow.planning.validation import PlanValidator
from agentflow.tools.registry import ToolRegistry

_REQUIRED_COLUMNS = {"date", "product", "quantity", "unit_price"}


class SalesDemoError(ValueError):
    """The uploaded CSV does not contain valid sales demo data."""


def supports_sales_demo(dataset: UploadedDataset) -> bool:
    """Return whether the uploaded dataset has the fields used by the sales demo."""
    return _REQUIRED_COLUMNS.issubset(set(dataset.frame.columns))


def create_sales_demo_plan(
    dataset: UploadedDataset,
    workspace: str | Path,
    registry: ToolRegistry,
) -> tuple[PlanningResult, UploadedDataset]:
    """Prepare revenue/month fields and return a strictly validated registered-tool plan."""
    if not supports_sales_demo(dataset):
        raise SalesDemoError(
            "The sales demo needs date, product, quantity, and unit_price columns."
        )

    if {"revenue", "month"}.issubset(set(dataset.frame.columns)):
        prepared = dataset
    else:
        frame = dataset.frame.copy()
        quantity = pd.to_numeric(frame["quantity"], errors="coerce")
        unit_price = pd.to_numeric(frame["unit_price"], errors="coerce")
        dates = pd.to_datetime(frame["date"], errors="coerce", format="mixed")
        if quantity.isna().any() or unit_price.isna().any() or dates.isna().any():
            raise SalesDemoError(
                "Sales demo rows must have numeric quantity and unit_price values and valid dates."
            )
        frame["revenue"] = quantity * unit_price
        frame["month"] = dates.dt.strftime("%Y-%m")
        prepared = validate_and_store_csv(
            "sales_demo_enriched.csv",
            frame.to_csv(index=False).encode("utf-8"),
            workspace,
        )

    path = prepared.relative_path
    plan = ExecutionPlan(
        objective_summary=(
            "Profile the uploaded sales dataset, aggregate revenue by product and month, "
            "and identify the top product from the product revenue results."
        ),
        steps=[
            PlanStep(
                step_key="profile_sales",
                tool_name="csv_profiler",
                purpose="Profile the validated sales dataset.",
                arguments={"path": path},
            ),
            PlanStep(
                step_key="revenue_by_product",
                tool_name="data_aggregator",
                purpose="Sum revenue for each product.",
                arguments={
                    "path": path,
                    "group_by": ["product"],
                    "aggregations": [{"column": "revenue", "operation": "sum", "alias": "revenue"}],
                },
            ),
            PlanStep(
                step_key="revenue_by_month",
                tool_name="data_aggregator",
                purpose="Sum revenue for each calendar month.",
                arguments={
                    "path": path,
                    "group_by": ["month"],
                    "aggregations": [{"column": "revenue", "operation": "sum", "alias": "revenue"}],
                },
            ),
        ],
        completion_criteria=[
            "The dataset profile and both aggregations succeed.",
            "The product with the highest aggregated revenue is shown in results.",
        ],
    )
    validated_plan = PlanValidator(registry).validate(plan)
    objective = (
        f"{validated_plan.objective_summary} Dataset: {prepared.relative_path}. "
        "This plan was prepared deterministically for the sales demonstration."
    )
    result = PlanningResult(
        objective=objective,
        plan=validated_plan,
        planner_kind=PlannerKind.RULE_BASED,
        fallback_reason="deterministic_sales_demo",
        duration_ms=0,
    )
    return result, prepared
