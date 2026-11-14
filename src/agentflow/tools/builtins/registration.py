"""Factory for an isolated registry containing the implemented built-ins."""

from agentflow.tools.builtins.calculator import calculator_definition
from agentflow.tools.builtins.csv_analysis import (
    csv_profiler_definition,
    data_aggregator_definition,
    duplicate_detector_definition,
    missing_analyzer_definition,
    outlier_detector_definition,
)
from agentflow.tools.builtins.dates import date_calculate_definition, date_difference_definition
from agentflow.tools.builtins.exporters import (
    json_exporter_definition,
    markdown_report_definition,
)
from agentflow.tools.registry import ToolRegistry


def create_builtin_registry(*, max_concurrent: int = 8) -> ToolRegistry:
    """Return a fresh registry with the approved built-in tool implementations."""
    registry = ToolRegistry(max_concurrent=max_concurrent)
    definitions = (
        calculator_definition(),
        date_difference_definition(),
        date_calculate_definition(),
        csv_profiler_definition(),
        missing_analyzer_definition(),
        duplicate_detector_definition(),
        outlier_detector_definition(),
        data_aggregator_definition(),
        json_exporter_definition(),
        markdown_report_definition(),
    )
    for definition in definitions:
        registry.register(definition)
    return registry
