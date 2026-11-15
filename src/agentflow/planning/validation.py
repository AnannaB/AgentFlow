"""Validate model-proposed plans against registered tool contracts and policy."""

from __future__ import annotations

import json

from agentflow.domain.models import ExecutionPlan, PlanStep
from agentflow.planning.errors import PlanValidationError
from agentflow.tools.errors import ToolError
from agentflow.tools.registry import ToolRegistry

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class PlanValidator:
    """Enforce plan structure, tool allowlisting, schema validity, and risk floors."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate(self, value: str | bytes | dict[str, object] | ExecutionPlan) -> ExecutionPlan:
        """Parse, validate and normalize risk levels before a plan can be used."""
        try:
            if isinstance(value, ExecutionPlan):
                plan = ExecutionPlan.model_validate_json(value.model_dump_json())
            elif isinstance(value, (str, bytes)):
                plan = ExecutionPlan.model_validate_json(value)
            else:
                plan = ExecutionPlan.model_validate_json(
                    json.dumps(value, ensure_ascii=False, allow_nan=False)
                )
        except Exception as error:
            raise PlanValidationError(_validation_message(error)) from None

        normalized_steps: list[PlanStep] = []
        for step in plan.steps:
            try:
                definition = self._registry.get(step.tool_name)
            except ToolError:
                raise PlanValidationError(
                    f"Step {step.step_key!r} selected an unregistered tool."
                ) from None
            try:
                encoded_arguments = json.dumps(
                    step.arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                definition.input_model.model_validate_json(encoded_arguments)
            except Exception as error:
                raise PlanValidationError(
                    f"Arguments for step {step.step_key!r} do not match the "
                    f"'{step.tool_name}' input schema: {_validation_message(error)}"
                ) from None

            tool_risk = definition.metadata.risk.value
            if definition.metadata.requires_approval and tool_risk == "low":
                tool_risk = "medium"
            risk = (
                tool_risk
                if _RISK_ORDER[tool_risk] > _RISK_ORDER[step.risk_level]
                else step.risk_level
            )
            normalized_steps.append(step.model_copy(update={"risk_level": risk}))

        return plan.model_copy(update={"steps": normalized_steps})


def _validation_message(error: Exception) -> str:
    """Format safe Pydantic/JSON diagnostics without echoing input values."""
    errors = getattr(error, "errors", None)
    if callable(errors):
        details = errors(include_input=False, include_url=False)
        concise = [
            f"{'.'.join(str(part) for part in item.get('loc', ())) or 'plan'}: "
            f"{item.get('msg', 'invalid value')}"
            for item in details[:5]
        ]
        if concise:
            return "; ".join(concise)
    return "The response was not valid plan JSON."
