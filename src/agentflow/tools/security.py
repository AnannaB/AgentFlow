"""Workspace path confinement for tools that access the filesystem."""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolInputError, ToolPermissionError


def resolve_workspace_path(
    value: str | os.PathLike[str],
    context: ToolExecutionContext,
    *,
    must_exist: bool,
    tool_name: str,
) -> Path:
    """Resolve a path and ensure it stays inside one of the configured roots."""
    try:
        path_value = os.fspath(value)
    except TypeError:
        path_value = ""
    if (
        not isinstance(path_value, str)
        or not path_value
        or len(path_value) > 4096
        or "\x00" in path_value
    ):
        raise ToolInputError("Path must contain 1 to 4096 valid characters", tool_name=tool_name)
    # Treat both slash conventions as separators on every OS. This prevents a
    # Windows-style path from being interpreted as a harmless POSIX filename.
    portable_value = path_value.replace("\\", "/")
    posix_path = PurePosixPath(portable_value)
    windows_path = PureWindowsPath(path_value)
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ToolPermissionError("Parent-directory traversal is not allowed", tool_name=tool_name)
    if not context.workspace_roots:
        raise ToolPermissionError("No workspace roots are configured", tool_name=tool_name)

    raw_path = Path(portable_value).expanduser()
    if os.name != "nt" and (windows_path.drive or windows_path.root) and not raw_path.is_absolute():
        raise ToolPermissionError(
            "Absolute paths must use the host path format", tool_name=tool_name
        )

    roots: list[Path] = []
    for configured_root in context.workspace_roots:
        try:
            root = Path(configured_root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ToolPermissionError("No valid workspace roots are configured", tool_name=tool_name)

    candidates = [raw_path] if raw_path.is_absolute() else [roots[0] / raw_path]
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as error:
            if must_exist:
                continue
            raise ToolInputError("Path cannot be resolved", tool_name=tool_name) from error
        if not any(_is_within(resolved, root) for root in roots):
            continue
        if must_exist and (not resolved.exists() or not resolved.is_file()):
            raise ToolInputError("Input path must be an existing file", tool_name=tool_name)
        if not must_exist and resolved.exists() and resolved.is_dir():
            raise ToolInputError("Output path must refer to a file", tool_name=tool_name)
        return resolved
    raise ToolPermissionError("Path is outside configured workspace roots", tool_name=tool_name)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
