"""Behavioral tests for the registered deterministic built-in tools."""

import json
from pathlib import Path

import pandas as pd
import pytest

from agentflow.tools.builtins.registration import create_builtin_registry
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import (
    ToolApprovalRequired,
    ToolExecutionError,
    ToolInputError,
    ToolPermissionError,
)


@pytest.fixture
def registry():
    return create_builtin_registry()


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.csv"
    pd.DataFrame(
        {
            "group": ["a", "a", "b", "b", "b"],
            "value": [1, 1, 2, 3, 100],
            "note": ["x", None, "z", None, "q"],
        }
    ).to_csv(path, index=False)
    return path


def ctx(root: Path, *, approved=False):
    return ToolExecutionContext(workspace_roots=(root,), approved=approved)


def test_calculator_and_dates(registry, tmp_path):
    assert registry.invoke("calculator", {"expression": "(2 + 3) * 4"}).result == 20
    with pytest.raises(ToolExecutionError, match="Division by zero"):
        registry.invoke("calculator", {"expression": "1 / 0"})
    with pytest.raises(ToolInputError):
        registry.invoke("calculator", {"expression": "__import__('os').system('whoami')"})
    assert (
        registry.invoke(
            "date_difference", {"start_date": "2024-01-01", "end_date": "2024-01-10"}
        ).difference_days
        == 9
    )
    assert (
        registry.invoke(
            "date_calculate", {"start_date": "2024-01-01", "offset_days": -1}
        ).result_date.isoformat()
        == "2023-12-31"
    )


def test_csv_profile_missing_duplicate_outliers_and_aggregation(registry, csv_file, tmp_path):
    context = ctx(tmp_path)
    profile = registry.invoke("csv_profiler", {"path": str(csv_file)}, context=context)
    assert (profile.row_count, profile.column_count) == (5, 3)
    assert profile.column_names == ["group", "value", "note"]
    missing = registry.invoke("missing_value_analyzer", {"path": str(csv_file)}, context=context)
    assert missing.total_missing == 2
    note_stats = next(item for item in missing.columns if item.column == "note")
    assert note_stats.missing_percentage == pytest.approx(40)
    duplicates = registry.invoke(
        "duplicate_detector", {"path": str(csv_file), "subset": ["group", "value"]}, context=context
    )
    assert duplicates.duplicate_count == 1
    outliers = registry.invoke(
        "outlier_detector", {"path": str(csv_file), "columns": ["value"]}, context=context
    )
    assert outliers.columns[0].outlier_count == 1
    aggregate = registry.invoke(
        "data_aggregator",
        {
            "path": str(csv_file),
            "group_by": ["group"],
            "aggregations": [
                {"column": "value", "operation": "sum", "alias": "total"},
                {"column": "value", "operation": "mean", "alias": "average"},
                {"operation": "count", "alias": "records"},
            ],
        },
        context=context,
    )
    assert aggregate.group_count == 2
    assert next(row for row in aggregate.rows if row["group"] == "a")["total"] == 2
    counts = registry.invoke(
        "data_aggregator",
        {
            "path": str(csv_file),
            "group_by": ["group"],
            "aggregations": [{"column": "note", "operation": "count"}],
        },
        context=context,
    )
    assert next(row for row in counts.rows if row["group"] == "a")["count_note"] == 1
    with pytest.raises(ToolInputError, match="must be numeric"):
        registry.invoke(
            "data_aggregator",
            {
                "path": str(csv_file),
                "group_by": ["group"],
                "aggregations": [{"column": "note", "operation": "mean"}],
            },
            context=context,
        )


def test_csv_edge_cases_and_path_restrictions(registry, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    empty = workspace / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ToolInputError):
        registry.invoke("csv_profiler", {"path": str(empty)}, context=ctx(workspace))
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n", encoding="utf-8")
    with pytest.raises(ToolPermissionError):
        registry.invoke("csv_profiler", {"path": str(outside)}, context=ctx(workspace))
    malformed = workspace / "malformed.csv"
    malformed.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ToolInputError):
        registry.invoke("csv_profiler", {"path": str(malformed)}, context=ctx(workspace))


def test_header_only_csv_and_aggregation_with_missing_groups(registry, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    empty = workspace / "header_only.csv"
    empty.write_text("group,value,note\n", encoding="utf-8")
    context = ctx(workspace)
    profile = registry.invoke("csv_profiler", {"path": str(empty)}, context=context)
    assert profile.row_count == 0
    assert profile.column_count == 3
    missing = registry.invoke("missing_value_analyzer", {"path": str(empty)}, context=context)
    assert missing.total_missing == 0
    assert missing.overall_missing_percentage == 0
    aggregate = registry.invoke(
        "data_aggregator",
        {
            "path": str(empty),
            "group_by": ["group"],
            "aggregations": [{"column": "value", "operation": "sum"}],
        },
        context=context,
    )
    assert aggregate.group_count == 0
    assert aggregate.rows == []


def test_json_export_approval_atomic_content_and_restrictions(registry, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    arguments = {"output_path": "reports/result.json", "payload": {"ok": True, "count": 3}}
    export_metadata = registry.get("json_exporter").metadata
    assert export_metadata.requires_approval is True
    assert export_metadata.retryable is False
    assert export_metadata.idempotent is False
    with pytest.raises(ToolApprovalRequired):
        registry.invoke("json_exporter", arguments, context=ctx(workspace))
    result = registry.invoke("json_exporter", arguments, context=ctx(workspace, approved=True))
    destination = workspace / "reports" / "result.json"
    assert json.loads(destination.read_text(encoding="utf-8")) == arguments["payload"]
    assert result.relative_path == "reports/result.json"
    with pytest.raises(ToolPermissionError):
        registry.invoke(
            "json_exporter",
            {"output_path": "../escape.json", "payload": {}},
            context=ctx(workspace, approved=True),
        )
    outside = tmp_path / "outside.json"
    outside.write_text('{"untouched": true}', encoding="utf-8")
    with pytest.raises(ToolPermissionError):
        registry.invoke(
            "json_exporter",
            {"output_path": str(outside), "payload": {"changed": True}},
            context=ctx(workspace, approved=True),
        )
    assert json.loads(outside.read_text(encoding="utf-8")) == {"untouched": True}
    with pytest.raises(ToolInputError):
        registry.invoke(
            "json_exporter",
            {"output_path": "invalid.json", "payload": {"number": float("nan")}},
            context=ctx(workspace, approved=True),
        )
    assert not (workspace / "invalid.json").exists()


def test_markdown_report_escapes_untrusted_values(registry):
    report = registry.invoke(
        "markdown_report_generator",
        {
            "title": "<script>alert(1)</script>",
            "summary": "A *summary*",
            "findings": [{"title": "Finding", "description": "safe [text](javascript:bad)"}],
        },
    )
    assert "<script>" not in report.markdown
    assert "\\*summary\\*" in report.markdown
    assert "javascript" in report.markdown
