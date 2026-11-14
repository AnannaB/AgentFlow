"""Strict input/output contracts for built-in tools."""

from datetime import date
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from agentflow.tools.base import ToolInput, ToolOutput


class CalculatorInput(ToolInput):
    expression: str = Field(min_length=1, max_length=500)


class CalculatorOutput(ToolOutput):
    expression: str
    result: float


class DateDifferenceInput(ToolInput):
    start_date: date
    end_date: date


class DateDifferenceOutput(ToolOutput):
    start_date: date
    end_date: date
    difference_days: int


class DateCalculationInput(ToolInput):
    start_date: date
    offset_days: int = Field(ge=-36500, le=36500)


class DateCalculationOutput(ToolOutput):
    result_date: date


class CSVPathInput(ToolInput):
    path: str = Field(min_length=1, max_length=4096)


class CSVProfileOutput(ToolOutput):
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    column_names: list[str]
    data_types: dict[str, str]
    missing_values: dict[str, int]
    descriptive_statistics: dict[str, dict[str, JsonValue]]


class MissingColumnStatistics(ToolOutput):
    column: str
    missing_count: int = Field(ge=0)
    missing_percentage: float = Field(ge=0, le=100)


class MissingValueOutput(ToolOutput):
    row_count: int = Field(ge=0)
    total_missing: int = Field(ge=0)
    overall_missing_percentage: float = Field(ge=0, le=100)
    columns: list[MissingColumnStatistics]


class DuplicateDetectorInput(CSVPathInput):
    subset: list[str] | None = Field(default=None, max_length=256)
    keep: Literal["first", "last", "all"] = "first"

    @model_validator(mode="after")
    def subset_columns_are_unique(self) -> "DuplicateDetectorInput":
        if self.subset is not None and len(self.subset) != len(set(self.subset)):
            raise ValueError("subset columns must be unique")
        return self


class DuplicateDetectorOutput(ToolOutput):
    row_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    duplicate_indices: list[int] = Field(max_length=1000)
    indices_truncated: bool = False


class OutlierDetectorInput(CSVPathInput):
    columns: list[str] | None = Field(default=None, max_length=256)
    iqr_multiplier: float = Field(default=1.5, gt=0, le=10)

    @model_validator(mode="after")
    def columns_are_unique(self) -> "OutlierDetectorInput":
        if self.columns is not None and len(self.columns) != len(set(self.columns)):
            raise ValueError("columns must be unique")
        return self


class OutlierColumnResult(ToolOutput):
    column: str
    lower_bound: float | None = None
    upper_bound: float | None = None
    outlier_count: int = Field(ge=0)
    row_indices: list[int] = Field(max_length=1000)
    indices_truncated: bool = False


class OutlierDetectorOutput(ToolOutput):
    row_count: int = Field(ge=0)
    columns: list[OutlierColumnResult]


class AggregationSpec(ToolInput):
    column: str | None = Field(default=None, max_length=256)
    operation: Literal["sum", "mean", "count", "min", "max"]
    alias: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def column_is_required_except_for_count(self) -> "AggregationSpec":
        if self.operation != "count" and not self.column:
            raise ValueError("column is required for sum, mean, min, and max")
        return self


class DataAggregatorInput(CSVPathInput):
    group_by: list[str] = Field(min_length=1, max_length=32)
    aggregations: list[AggregationSpec] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def aliases_are_unique(self) -> "DataAggregatorInput":
        aliases = [
            spec.alias or f"{spec.operation}_{spec.column or 'rows'}" for spec in self.aggregations
        ]
        if len(aliases) != len(set(aliases)):
            raise ValueError("aggregation output aliases must be unique")
        if len(self.group_by) != len(set(self.group_by)):
            raise ValueError("group_by columns must be unique")
        if set(aliases) & set(self.group_by):
            raise ValueError("aggregation aliases must not overlap group_by columns")
        return self


class DataAggregatorOutput(ToolOutput):
    group_count: int = Field(ge=0)
    columns: list[str]
    rows: list[dict[str, JsonValue]] = Field(max_length=10000)


class JSONExportInput(ToolInput):
    output_path: str = Field(min_length=1, max_length=4096)
    payload: JsonValue


class JSONExportOutput(ToolOutput):
    relative_path: str
    bytes_written: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportFinding(ToolInput):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2000)


class MarkdownReportInput(ToolInput):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    profile: CSVProfileOutput | None = None
    findings: list[ReportFinding] = Field(default_factory=list, max_length=100)


class MarkdownReportOutput(ToolOutput):
    markdown: str = Field(max_length=64000)
    word_count: int = Field(ge=0)
