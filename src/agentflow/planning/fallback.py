"""Small, deterministic fallback rules for clearly recognized objectives."""

from __future__ import annotations

import re

from agentflow.domain.models import ExecutionPlan, PlanStep
from agentflow.planning.errors import FallbackUnavailableError
from agentflow.tools.errors import ToolError
from agentflow.tools.registry import ToolRegistry

_DATE_DIFFERENCE = re.compile(
    r"(?:difference|days? between).*?(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_DATE_CALCULATION = re.compile(
    r"\b(add|subtract)\s+(\d{1,5})\s+days?\s+"
    r"(?:to|from)\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_CALCULATION = re.compile(
    r"\b(?:calculate|compute|evaluate|what is)\s+"
    r"([0-9][0-9\s()+\-*/%.]*[0-9)])",
    re.IGNORECASE,
)


class RuleBasedPlanner:
    """Handle only basic arithmetic and explicit ISO date arithmetic intents."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def plan(self, objective: str) -> ExecutionPlan:
        """Produce a one-step plan for a narrow recognized intent or fail clearly."""
        difference = _DATE_DIFFERENCE.search(objective)
        if difference:
            tool_name = "date_difference"
            arguments = {"start_date": difference.group(1), "end_date": difference.group(2)}
            purpose = "Calculate the difference between the requested dates."
        else:
            calculation = _DATE_CALCULATION.search(objective)
            if calculation:
                operation, amount, date = calculation.groups()
                offset = int(amount) * (1 if operation.lower() == "add" else -1)
                tool_name = "date_calculate"
                arguments = {"start_date": date, "offset_days": offset}
                purpose = "Apply the requested day offset to the given date."
            else:
                arithmetic = _CALCULATION.search(objective)
                if not arithmetic:
                    raise FallbackUnavailableError(
                        "Rule-based fallback supports explicit arithmetic and "
                        "ISO date calculations."
                    )
                tool_name = "calculator"
                arguments = {"expression": arithmetic.group(1).strip()}
                purpose = "Evaluate the explicit arithmetic expression in the objective."

        try:
            self._registry.get(tool_name)
        except ToolError:
            raise FallbackUnavailableError(
                f"Rule-based fallback requires the registered '{tool_name}' tool."
            ) from None
        return ExecutionPlan(
            objective_summary=objective[:2000],
            steps=[
                PlanStep(
                    step_key="fallback_step",
                    tool_name=tool_name,
                    purpose=purpose,
                    arguments=arguments,
                )
            ],
            completion_criteria=["The selected registered tool completes successfully."],
        )
