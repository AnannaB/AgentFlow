"""Workspace-confined JSON export and readable Markdown report generation."""

import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path

from agentflow.tools.base import ToolDefinition, ToolMetadata, ToolRisk
from agentflow.tools.builtins.models import (
    JSONExportInput,
    JSONExportOutput,
    MarkdownReportInput,
    MarkdownReportOutput,
)
from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolInputError
from agentflow.tools.security import resolve_workspace_path

_VERSION = "1.0.0"
_MAX_EXPORT_BYTES = 1024 * 1024
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def export_json(data: JSONExportInput, context: ToolExecutionContext) -> JSONExportOutput:
    """Write validated JSON atomically to a path under a configured workspace root."""
    context.checkpoint("json_exporter")
    destination = resolve_workspace_path(
        data.output_path,
        context,
        must_exist=False,
        tool_name="json_exporter",
    )
    if destination.suffix.lower() != ".json":
        raise ToolInputError(
            "JSON export path must use the .json extension", tool_name="json_exporter"
        )
    try:
        serialized = (
            json.dumps(
                data.payload,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n"
        )
    except (TypeError, ValueError):
        raise ToolInputError(
            "Payload must contain finite JSON values", tool_name="json_exporter"
        ) from None
    content = serialized.encode("utf-8")
    if len(content) > _MAX_EXPORT_BYTES:
        raise ToolInputError("JSON export exceeds the 1 MiB limit", tool_name="json_exporter")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ToolInputError(
            "JSON export directory could not be created", tool_name="json_exporter"
        ) from None
    destination = resolve_workspace_path(
        str(destination),
        context,
        must_exist=False,
        tool_name="json_exporter",
    )
    temporary_path: Path | None = None
    try:
        context.checkpoint("json_exporter")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".agentflow-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        context.checkpoint("json_exporter")
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError:
        raise ToolInputError(
            "JSON export could not be written", tool_name="json_exporter"
        ) from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # Cleanup failure must not replace the original typed tool error.
                pass

    root = next(root for root in context.workspace_roots if _is_within(destination, root))
    return JSONExportOutput(
        relative_path=destination.relative_to(root).as_posix(),
        bytes_written=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def generate_markdown_report(
    data: MarkdownReportInput, context: ToolExecutionContext
) -> MarkdownReportOutput:
    """Generate a Markdown summary while escaping untrusted input text."""
    context.checkpoint("markdown_report_generator")
    lines = [f"# {_markdown_text(data.title)}", "", _markdown_text(data.summary)]
    if data.profile is not None:
        profile = data.profile
        lines.extend(
            [
                "",
                "## Dataset overview",
                "",
                f"- Rows: {profile.row_count}",
                f"- Columns: {profile.column_count}",
                "- Column names: "
                + ", ".join(_markdown_text(name) for name in profile.column_names),
            ]
        )
    if data.findings:
        lines.extend(["", "## Findings", ""])
        for finding in data.findings:
            context.checkpoint("markdown_report_generator")
            lines.extend(
                [
                    f"### {_markdown_text(finding.title)}",
                    "",
                    _markdown_text(finding.description),
                    "",
                ]
            )
    markdown = "\n".join(lines).rstrip() + "\n"
    if len(markdown.encode("utf-8")) > 64 * 1024:
        raise ToolInputError(
            "Generated report exceeds the 64 KiB limit", tool_name="markdown_report_generator"
        )
    return MarkdownReportOutput(markdown=markdown, word_count=len(markdown.split()))


def json_exporter_definition() -> ToolDefinition[JSONExportInput, JSONExportOutput]:
    return ToolDefinition(
        metadata=ToolMetadata(
            name="json_exporter",
            version=_VERSION,
            description=(
                "Atomically export validated JSON under an explicitly configured workspace root."
            ),
            risk=ToolRisk.MEDIUM,
            timeout_seconds=30,
            retryable=False,
            idempotent=False,
            requires_approval=True,
        ),
        input_model=JSONExportInput,
        output_model=JSONExportOutput,
        handler=export_json,
    )


def markdown_report_definition() -> ToolDefinition[MarkdownReportInput, MarkdownReportOutput]:
    return ToolDefinition(
        metadata=ToolMetadata(
            name="markdown_report_generator",
            version=_VERSION,
            description=(
                "Create a readable Markdown analysis report from validated summaries and findings."
            ),
            risk=ToolRisk.LOW,
            timeout_seconds=5,
            retryable=True,
            idempotent=True,
        ),
        input_model=MarkdownReportInput,
        output_model=MarkdownReportOutput,
        handler=generate_markdown_report,
    )


def _markdown_text(value: str) -> str:
    flattened = " ".join(value.splitlines())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", html.escape(flattened, quote=True))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
