"""Validate model-proposed plans against registered tool contracts and policy."""

from __future__ import annotations

import json

from pydantic import ValidationError

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
        except ValidationError as error:
            raise PlanValidationError(
                _validation_message(error), **_plan_diagnostic(error)
            ) from None
        except Exception as error:
            raise PlanValidationError(
                _validation_message(error), diagnostic_code="plan_schema_invalid"
            ) from None

        normalized_steps: list[PlanStep] = []
        for step_index, step in enumerate(plan.steps):
            try:
                definition = self._registry.get(step.tool_name)
            except ToolError:
                raise PlanValidationError(
                    "A plan step selected an unregistered tool.",
                    diagnostic_code="unregistered_tool",
                    step_index=step_index,
                    field_path=f"steps[{step_index}].tool_name",
                ) from None
            try:
                encoded_arguments = json.dumps(
                    step.arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                definition.input_model.model_validate_json(encoded_arguments)
            except ValidationError as error:
                raise PlanValidationError(
                    f"Arguments for step {step.step_key!r} do not match the "
                    f"'{step.tool_name}' input schema: {_validation_message(error)}",
                    diagnostic_code="tool_input_schema_invalid",
                    step_index=step_index,
                    tool_name=definition.metadata.name,
                    field_path=_safe_field_path(
                        error.errors(include_input=False, include_url=False)[0].get("loc", ()),
                        allowed_fields=set(definition.input_model.model_fields),
                        prefix=f"steps[{step_index}].arguments",
                    ),
                ) from None
            except Exception:
                raise PlanValidationError(
                    f"Arguments for step {step.step_key!r} do not match the "
                    f"'{step.tool_name}' input schema.",
                    diagnostic_code="tool_input_schema_invalid",
                    step_index=step_index,
                    tool_name=definition.metadata.name,
                    field_path=f"steps[{step_index}].arguments",
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


def _plan_diagnostic(error: ValidationError) -> dict[str, object]:
    """Build a safe diagnostic without copying model-provided values into logs."""
    issues = error.errors(include_input=False, include_url=False)
    if not issues:
        return {"diagnostic_code": "plan_schema_invalid", "field_path": "plan"}

    issue = issues[0]
    location = issue.get("loc", ())
    error_type = issue.get("type")
    code = "malformed_plan_json" if error_type == "json_invalid" else "plan_schema_invalid"
    field_path = _safe_field_path(
        location,
        allowed_fields={
            "objective_summary",
            "steps",
            "step_key",
            "tool_name",
            "purpose",
            "arguments",
            "depends_on",
            "required",
            "risk_level",
            "completion_criteria",
            "requires_approval",
        },
        prefix="plan",
    )
    step_index = _step_index(location)

    if error_type == "value_error":
        detail = str(issue.get("ctx", {}).get("error", ""))
        if "unknown dependencies" in detail:
            code, field_path = "unknown_dependency", "steps.depends_on"
        elif "cannot depend on itself" in detail:
            code, field_path = "self_dependency", "steps.depends_on"
        elif "duplicate dependencies" in detail:
            code, field_path = "duplicate_dependency", "steps.depends_on"
        elif "must not contain a cycle" in detail:
            code, field_path = "dependency_cycle", "steps.depends_on"
        elif "step_key values must be unique" in detail:
            code, field_path = "duplicate_step_key", "steps.step_key"

    return {
        "diagnostic_code": code,
        "step_index": step_index,
        "field_path": field_path,
    }


def _step_index(location: object) -> int | None:
    if not isinstance(location, (tuple, list)):
        return None
    try:
        steps_position = location.index("steps")
    except ValueError:
        return None
    if steps_position + 1 >= len(location):
        return None
    index = location[steps_position + 1]
    return index if type(index) is int and 0 <= index < 100 else None


def _safe_field_path(
    location: object,
    *,
    allowed_fields: set[str],
    prefix: str,
) -> str:
    if not isinstance(location, (tuple, list)):
        return prefix
    safe_parts: list[str] = [prefix]
    for segment in location:
        if isinstance(segment, str) and segment in allowed_fields:
            safe_parts.append(segment)
        elif type(segment) is int and 0 <= segment < 100:
            if safe_parts[-1] == "steps":
                safe_parts[-1] = f"steps[{segment}]"
    return ".".join(safe_parts)
