"""Bounded CSV profiling and analysis tools implemented with pandas/NumPy."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, TypeVar

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from agentflow.tools.base import ToolDefinition, ToolInput, ToolMetadata, ToolOutput, ToolRisk
from agentflow.tools.builtins.models import (
    CSVPathInput,
    CSVProfileOutput,
    DataAggregatorInput,
    DataAggregatorOutput,
    DuplicateDetectorInput,
    DuplicateDetectorOutput,
    MissingColumnStatistics,
    MissingValueOutput,
    OutlierColumnResult,
    OutlierDetectorInput,
    OutlierDetectorOutput,
)
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolInputError
from agentflow.tools.security import resolve_workspace_path

_VERSION = "1.0.0"
_MAX_CSV_BYTES = 10 * 1024 * 1024
_MAX_CSV_ROWS = 100_000
_MAX_CSV_COLUMNS = 256
_MAX_INDEX_RESULTS = 1000
InputT = TypeVar("InputT", bound=ToolInput)
OutputT = TypeVar("OutputT", bound=ToolOutput)


def load_csv(path: str, context: ToolExecutionContext, *, tool_name: str) -> pd.DataFrame:
    """Read a UTF-8 CSV from an allowed workspace with hard size/shape limits."""
    context.checkpoint(tool_name)
    source = resolve_workspace_path(path, context, must_exist=True, tool_name=tool_name)
    try:
        if source.stat().st_size > _MAX_CSV_BYTES:
            raise ToolInputError(
                "CSV file exceeds the 10 MiB limit", tool_name=tool_name, code="input_too_large"
            )
        frame = pd.read_csv(
            source,
            encoding="utf-8",
            low_memory=False,
            nrows=_MAX_CSV_ROWS + 1,
        )
    except ToolInputError:
        raise
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError):
        raise ToolInputError(
            "CSV could not be read as a valid UTF-8 delimited file", tool_name=tool_name
        ) from None
    context.checkpoint(tool_name)
    if len(frame) > _MAX_CSV_ROWS:
        raise ToolInputError("CSV exceeds the 100,000 row limit", tool_name=tool_name)
    if len(frame.columns) > _MAX_CSV_COLUMNS:
        raise ToolInputError("CSV exceeds the 256 column limit", tool_name=tool_name)
    return frame


def profile_csv(data: CSVPathInput, context: ToolExecutionContext) -> CSVProfileOutput:
    frame = load_csv(data.path, context, tool_name="csv_profiler")
    missing = {str(column): int(frame[column].isna().sum()) for column in frame.columns}
    statistics: dict[str, dict[str, Any]] = {}
    for column in frame.columns:
        context.checkpoint("csv_profiler")
        series = frame[column]
        if is_numeric_dtype(series.dtype):
            clean = series.dropna()
            values = {
                "count": int(clean.count()),
                "mean": _safe_number(clean.mean()) if len(clean) else None,
                "standard_deviation": _safe_number(clean.std()) if len(clean) > 1 else None,
                "minimum": _safe_number(clean.min()) if len(clean) else None,
                "q1": _safe_number(clean.quantile(0.25)) if len(clean) else None,
                "median": _safe_number(clean.median()) if len(clean) else None,
                "q3": _safe_number(clean.quantile(0.75)) if len(clean) else None,
                "maximum": _safe_number(clean.max()) if len(clean) else None,
            }
        else:
            nonmissing = series.dropna()
            top = nonmissing.mode().iloc[0] if not nonmissing.empty else None
            top_count = int((nonmissing == top).sum()) if top is not None else 0
            values = {
                "count": int(nonmissing.count()),
                "unique": int(nonmissing.nunique()),
                "most_frequent": _json_value(top),
                "most_frequent_count": top_count,
            }
        statistics[str(column)] = values
    return CSVProfileOutput(
        row_count=int(frame.shape[0]),
        column_count=int(frame.shape[1]),
        column_names=[str(column) for column in frame.columns],
        data_types={str(column): str(dtype) for column, dtype in frame.dtypes.items()},
        missing_values=missing,
        descriptive_statistics=statistics,
    )


def analyze_missing_values(data: CSVPathInput, context: ToolExecutionContext) -> MissingValueOutput:
    frame = load_csv(data.path, context, tool_name="missing_value_analyzer")
    rows, columns = frame.shape
    stats = [
        MissingColumnStatistics(
            column=str(column),
            missing_count=count,
            missing_percentage=(count / rows * 100.0) if rows else 0.0,
        )
        for column in frame.columns
        if (count := int(frame[column].isna().sum())) >= 0
    ]
    total_missing = sum(item.missing_count for item in stats)
    cell_count = rows * columns
    return MissingValueOutput(
        row_count=int(rows),
        total_missing=total_missing,
        overall_missing_percentage=(total_missing / cell_count * 100.0) if cell_count else 0.0,
        columns=stats,
    )


def detect_duplicates(
    data: DuplicateDetectorInput, context: ToolExecutionContext
) -> DuplicateDetectorOutput:
    frame = load_csv(data.path, context, tool_name="duplicate_detector")
    subset = data.subset
    if subset is not None:
        _require_columns(frame, subset, "duplicate_detector")
        if not subset:
            raise ToolInputError(
                "subset must contain at least one column", tool_name="duplicate_detector"
            )
    keep: str | bool = False if data.keep == "all" else data.keep
    duplicate_mask = frame.duplicated(subset=subset, keep=keep)
    indices = np.flatnonzero(duplicate_mask.to_numpy()).tolist()
    return DuplicateDetectorOutput(
        row_count=int(len(frame)),
        duplicate_count=int(duplicate_mask.sum()),
        duplicate_indices=[int(index) for index in indices[:_MAX_INDEX_RESULTS]],
        indices_truncated=len(indices) > _MAX_INDEX_RESULTS,
    )


def detect_outliers(
    data: OutlierDetectorInput, context: ToolExecutionContext
) -> OutlierDetectorOutput:
    frame = load_csv(data.path, context, tool_name="outlier_detector")
    if data.columns == []:
        raise ToolInputError("columns must not be empty", tool_name="outlier_detector")
    selected = data.columns or [
        str(column) for column in frame.select_dtypes(include="number").columns
    ]
    _require_columns(frame, selected, "outlier_detector")
    results: list[OutlierColumnResult] = []
    for column in selected:
        context.checkpoint("outlier_detector")
        series = frame[column]
        if not is_numeric_dtype(series.dtype):
            raise ToolInputError(f"Column {column!r} is not numeric", tool_name="outlier_detector")
        clean = series.dropna()
        if clean.empty:
            results.append(
                OutlierColumnResult(
                    column=column,
                    outlier_count=0,
                    row_indices=[],
                )
            )
            continue
        q1 = float(clean.quantile(0.25))
        q3 = float(clean.quantile(0.75))
        spread = q3 - q1
        lower = q1 - data.iqr_multiplier * spread
        upper = q3 + data.iqr_multiplier * spread
        mask = series.lt(lower) | series.gt(upper)
        indices = np.flatnonzero(mask.fillna(False).to_numpy()).tolist()
        results.append(
            OutlierColumnResult(
                column=column,
                lower_bound=_safe_number(lower),
                upper_bound=_safe_number(upper),
                outlier_count=len(indices),
                row_indices=[int(index) for index in indices[:_MAX_INDEX_RESULTS]],
                indices_truncated=len(indices) > _MAX_INDEX_RESULTS,
            )
        )
    return OutlierDetectorOutput(row_count=int(len(frame)), columns=results)


def aggregate_data(
    data: DataAggregatorInput, context: ToolExecutionContext
) -> DataAggregatorOutput:
    frame = load_csv(data.path, context, tool_name="data_aggregator")
    _require_columns(frame, data.group_by, "data_aggregator")
    for spec in data.aggregations:
        if spec.column is not None:
            _require_columns(frame, [spec.column], "data_aggregator")
            if (
                not frame.empty
                and spec.operation in {"sum", "mean"}
                and not is_numeric_dtype(frame[spec.column].dtype)
            ):
                raise ToolInputError(
                    f"Column {spec.column!r} must be numeric for {spec.operation}",
                    tool_name="data_aggregator",
                )
    grouped = frame.groupby(data.group_by, dropna=False, sort=True)
    group_sizes = grouped.size()
    result = pd.DataFrame(index=group_sizes.index)
    for spec in data.aggregations:
        context.checkpoint("data_aggregator")
        alias = spec.alias or f"{spec.operation}_{spec.column or 'rows'}"
        if spec.operation == "count" and spec.column is None:
            result[alias] = group_sizes
        else:
            assert spec.column is not None
            result[alias] = grouped[spec.column].agg(spec.operation)
    result = result.reset_index()
    if len(result) > 10_000:
        raise ToolInputError(
            "Aggregation output exceeds the 10,000 group limit", tool_name="data_aggregator"
        )
    rows = [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in result.to_dict("records")
    ]
    columns = [str(column) for column in result.columns]
    return DataAggregatorOutput(group_count=len(rows), columns=columns, rows=rows)


def _require_columns(frame: pd.DataFrame, requested: list[str], tool_name: str) -> None:
    missing = [column for column in requested if column not in frame.columns]
    if missing:
        raise ToolInputError(
            "One or more requested columns do not exist",
            tool_name=tool_name,
            code="unknown_column",
        )


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def csv_profiler_definition() -> ToolDefinition[CSVPathInput, CSVProfileOutput]:
    return _csv_definition(
        "csv_profiler",
        "Profile CSV dimensions, types, missing values, and statistics.",
        CSVPathInput,
        CSVProfileOutput,
        profile_csv,
    )


def missing_analyzer_definition() -> ToolDefinition[CSVPathInput, MissingValueOutput]:
    return _csv_definition(
        "missing_value_analyzer",
        "Count missing values and calculate percentages for every CSV column.",
        CSVPathInput,
        MissingValueOutput,
        analyze_missing_values,
    )


def duplicate_detector_definition() -> ToolDefinition[
    DuplicateDetectorInput, DuplicateDetectorOutput
]:
    return _csv_definition(
        "duplicate_detector",
        "Detect duplicate CSV rows, optionally comparing selected columns.",
        DuplicateDetectorInput,
        DuplicateDetectorOutput,
        detect_duplicates,
    )


def outlier_detector_definition() -> ToolDefinition[OutlierDetectorInput, OutlierDetectorOutput]:
    return _csv_definition(
        "outlier_detector",
        "Find numeric CSV outliers with configurable IQR bounds.",
        OutlierDetectorInput,
        OutlierDetectorOutput,
        detect_outliers,
    )


def data_aggregator_definition() -> ToolDefinition[DataAggregatorInput, DataAggregatorOutput]:
    return _csv_definition(
        "data_aggregator",
        "Group CSV rows and calculate sum, mean, count, minimum, or maximum.",
        DataAggregatorInput,
        DataAggregatorOutput,
        aggregate_data,
    )


def _csv_definition[InputT: ToolInput, OutputT: ToolOutput](
    name: str,
    description: str,
    input_model: type[InputT],
    output_model: type[OutputT],
    handler: Callable[[InputT, ToolExecutionContext], OutputT],
) -> ToolDefinition[InputT, OutputT]:
    return ToolDefinition(
        metadata=ToolMetadata(
            name=name,
            version=_VERSION,
            description=description,
            risk=ToolRisk.LOW,
            timeout_seconds=30,
            retryable=True,
            idempotent=True,
        ),
        input_model=input_model,
        output_model=output_model,
        handler=handler,
    )
