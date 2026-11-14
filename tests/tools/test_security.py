"""Workspace confinement and tool context checks."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolPermissionError
from agentflow.tools.security import resolve_workspace_path


def test_workspace_path_allows_inside_and_rejects_traversal(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolExecutionContext(workspace_roots=(workspace,))
    assert (
        resolve_workspace_path("data.csv", context, must_exist=False, tool_name="test").parent
        == workspace
    )
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path("../outside.json", context, must_exist=False, tool_name="test")
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path(
            "nested/../../outside.json", context, must_exist=False, tool_name="test"
        )
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path(
            r"nested\..\..\outside.json", context, must_exist=False, tool_name="test"
        )


def test_absolute_outside_and_symlink_escape_are_rejected(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    context = ToolExecutionContext(workspace_roots=(workspace,))
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path(outside, context, must_exist=True, tool_name="test")
    link = workspace / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable for this account")
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path(link, context, must_exist=True, tool_name="test")


def test_paths_require_configured_roots_and_existing_input(tmp_path: Path):
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path(
            "data.csv", ToolExecutionContext(), must_exist=False, tool_name="test"
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ToolPermissionError):
        resolve_workspace_path(
            "missing.csv",
            ToolExecutionContext(workspace_roots=(workspace,)),
            must_exist=True,
            tool_name="test",
        )


def test_execution_context_security_fields_cannot_be_mutated(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolExecutionContext(workspace_roots=(workspace,))
    with pytest.raises(FrozenInstanceError):
        context.workspace_roots = (tmp_path,)
    with pytest.raises(FrozenInstanceError):
        context.approved = True
