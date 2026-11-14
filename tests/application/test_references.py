"""Tests for exact, dependency-scoped JSON Pointer references."""

import pytest

from agentflow.application.errors import InvalidOutputReferenceError
from agentflow.application.references import resolve_step_arguments


@pytest.fixture
def output_values():
    return {"source": {"data": {"items": [{"a/b": {"~key": None}, "n": 4}]}}}


def resolve(marker, outputs, dependencies=None):
    return resolve_step_arguments(
        marker,
        current_step_key="target",
        direct_dependencies=dependencies or {"source"},
        successful_outputs=outputs,
    )


def test_resolves_root_nested_array_and_pointer_escapes(output_values):
    assert resolve(
        {"copy": {"$agentflow_ref": {"step_key": "source", "pointer": "/data/items/0/a~1b/~0key"}}},
        output_values,
    ) == {"copy": None}
    assert (
        resolve(
            {"$agentflow_ref": {"step_key": "source", "pointer": "/data/items/0/n"}}, output_values
        )
        == 4
    )
    assert (
        resolve({"$agentflow_ref": {"step_key": "source", "pointer": ""}}, output_values)
        == output_values["source"]
    )


@pytest.mark.parametrize(
    "reference",
    [
        {"$agentflow_ref": {"step_key": "source"}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "/data", "extra": 1}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "data"}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "/data/~2"}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "/data/items/01"}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "/data/items/99"}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "/missing"}},
        {"$agentflow_ref": {"step_key": "other", "pointer": "/data"}},
        {"$agentflow_ref": {"step_key": "target", "pointer": "/data"}},
        {"$agentflow_ref": {"step_key": "source", "pointer": 4}},
        {"$agentflow_ref": {"step_key": "source", "pointer": "/data"}, "literal": True},
    ],
)
def test_rejects_malformed_missing_and_out_of_scope_references(reference, output_values):
    with pytest.raises(InvalidOutputReferenceError):
        resolve(reference, output_values)


def test_requires_successful_current_run_dependency_output(output_values):
    with pytest.raises(InvalidOutputReferenceError, match="successfully completed"):
        resolve(
            {"$agentflow_ref": {"step_key": "source", "pointer": "/data"}},
            {},
        )


def test_nested_arbitrary_expressions_remain_literal_data(output_values):
    expression = "__import__('os').system('whoami')"
    assert resolve({"text": expression}, output_values) == {"text": expression}
