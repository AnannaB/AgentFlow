"""Deterministic date difference and date arithmetic tools."""

from datetime import timedelta

from agentflow.tools.base import ToolDefinition, ToolMetadata, ToolRisk
from agentflow.tools.builtins.models import (
    DateCalculationInput,
    DateCalculationOutput,
    DateDifferenceInput,
    DateDifferenceOutput,
)
from agentflow.tools.context import ToolExecutionContext

_VERSION = "1.0.0"


def date_difference(
    data: DateDifferenceInput, context: ToolExecutionContext
) -> DateDifferenceOutput:
    context.checkpoint("date_difference")
    return DateDifferenceOutput(
        start_date=data.start_date,
        end_date=data.end_date,
        difference_days=(data.end_date - data.start_date).days,
    )


def add_days(data: DateCalculationInput, context: ToolExecutionContext) -> DateCalculationOutput:
    context.checkpoint("date_calculate")
    return DateCalculationOutput(result_date=data.start_date + timedelta(days=data.offset_days))


def date_difference_definition() -> ToolDefinition[DateDifferenceInput, DateDifferenceOutput]:
    return ToolDefinition(
        metadata=ToolMetadata(
            name="date_difference",
            version=_VERSION,
            description="Calculate signed whole-day difference between two ISO calendar dates.",
            risk=ToolRisk.LOW,
            timeout_seconds=2,
            retryable=True,
            idempotent=True,
        ),
        input_model=DateDifferenceInput,
        output_model=DateDifferenceOutput,
        handler=date_difference,
    )


def date_calculate_definition() -> ToolDefinition[DateCalculationInput, DateCalculationOutput]:
    return ToolDefinition(
        metadata=ToolMetadata(
            name="date_calculate",
            version=_VERSION,
            description="Add or subtract a bounded number of days from an ISO calendar date.",
            risk=ToolRisk.LOW,
            timeout_seconds=2,
            retryable=True,
            idempotent=True,
        ),
        input_model=DateCalculationInput,
        output_model=DateCalculationOutput,
        handler=add_days,
    )
