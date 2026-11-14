"""Safe arithmetic calculator based on a narrowly allowlisted Python AST."""

import ast
import math
import operator
from typing import cast

from agentflow.tools.base import ToolDefinition, ToolMetadata, ToolRisk
from agentflow.tools.builtins.models import CalculatorInput, CalculatorOutput
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolExecutionError, ToolInputError

_VERSION = "1.0.0"
_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX_ABS_RESULT = 1e100
_MAX_INTEGER_BITS = 4096


def calculate(data: CalculatorInput, context: ToolExecutionContext) -> CalculatorOutput:
    context.checkpoint("calculator")
    try:
        tree = ast.parse(data.expression, mode="eval")
    except (SyntaxError, ValueError):
        raise ToolInputError(
            "Expression is not valid arithmetic",
            tool_name="calculator",
            tool_version=_VERSION,
        ) from None
    nodes = list(ast.walk(tree))
    if len(nodes) > 64:
        raise ToolInputError(
            "Expression is too complex",
            tool_name="calculator",
            tool_version=_VERSION,
        )
    try:
        result = _evaluate(tree.body, context)
    except ZeroDivisionError:
        raise ToolExecutionError(
            "Division by zero",
            tool_name="calculator",
            tool_version=_VERSION,
            code="division_by_zero",
        ) from None
    except (OverflowError, ValueError):
        raise ToolExecutionError(
            "Expression result is outside the supported numeric range",
            tool_name="calculator",
            tool_version=_VERSION,
            code="numeric_range",
        ) from None
    return CalculatorOutput(expression=data.expression, result=float(result))


def _evaluate(node: ast.AST, context: ToolExecutionContext) -> int | float:
    context.checkpoint("calculator")
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value = cast(int | float, node.value)
        if not math.isfinite(value) or abs(value) > _MAX_ABS_RESULT:
            raise ValueError("number is outside supported range")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        result = _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand, context))
        return _check_result(result)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left, context)
        right = _evaluate(node.right, context)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent is outside supported range")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        return _check_result(result)
    raise ToolInputError(
        "Only numeric literals and basic arithmetic operators are allowed",
        tool_name="calculator",
        tool_version=_VERSION,
    )


def _check_result(value: int | float) -> int | float:
    if isinstance(value, complex):
        raise ValueError("complex results are not supported")
    if isinstance(value, int) and value.bit_length() > _MAX_INTEGER_BITS:
        raise ValueError("integer result is too large")
    if not math.isfinite(value) or abs(value) > _MAX_ABS_RESULT:
        raise ValueError("result is outside supported range")
    return value


def calculator_definition() -> ToolDefinition[CalculatorInput, CalculatorOutput]:
    return ToolDefinition(
        metadata=ToolMetadata(
            name="calculator",
            version=_VERSION,
            description="Evaluate safe basic arithmetic expressions without executing code.",
            risk=ToolRisk.LOW,
            timeout_seconds=2,
            retryable=True,
            idempotent=True,
        ),
        input_model=CalculatorInput,
        output_model=CalculatorOutput,
        handler=calculate,
    )
