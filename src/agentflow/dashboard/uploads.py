"""Safe CSV upload validation and workspace storage for the dashboard."""

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pandas as pd

from agentflow.tools.context import ToolExecutionContext
from agentflow.tools.errors import ToolError
from agentflow.tools.security import resolve_workspace_path

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_ROWS = 100_000
MAX_UPLOAD_COLUMNS = 256


class UploadValidationError(ValueError):
    """Raised when a user upload is not a supported bounded CSV file."""


@dataclass(frozen=True, slots=True)
class UploadedDataset:
    """Validated uploaded CSV path and in-memory preview data."""

    path: Path
    relative_path: str
    frame: pd.DataFrame
    byte_count: int


def validate_and_store_csv(filename: str, content: bytes, workspace: str | Path) -> UploadedDataset:
    """Validate an uploaded UTF-8 CSV and store it under an isolated workspace name."""
    if not filename.lower().endswith(".csv"):
        raise UploadValidationError("Upload a file with the .csv extension.")
    if not content:
        raise UploadValidationError("The uploaded CSV is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadValidationError("CSV uploads are limited to 10 MiB.")

    try:
        frame = pd.read_csv(BytesIO(content), encoding="utf-8-sig", nrows=MAX_UPLOAD_ROWS + 1)
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError, ValueError):
        raise UploadValidationError("The upload is not a valid UTF-8 CSV file.") from None
    if len(frame) > MAX_UPLOAD_ROWS:
        raise UploadValidationError("CSV uploads are limited to 100,000 rows.")
    if len(frame.columns) > MAX_UPLOAD_COLUMNS:
        raise UploadValidationError("CSV uploads are limited to 256 columns.")
    if frame.empty or len(frame.columns) == 0:
        raise UploadValidationError("The CSV must contain a header and at least one data row.")

    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise UploadValidationError("The configured workspace is not a directory.")
    try:
        upload_dir = root / "uploads"
        upload_dir.mkdir(exist_ok=True)
        safe_name = _safe_filename(filename)
        relative_path = Path("uploads") / f"{uuid4().hex}_{safe_name}"
        context = ToolExecutionContext(workspace_roots=(root,))
        target = resolve_workspace_path(
            str(relative_path), context, must_exist=False, tool_name="dashboard_upload"
        )
        target.write_bytes(content)
    except ToolError:
        raise UploadValidationError(
            "The upload destination is outside the configured workspace."
        ) from None
    except OSError:
        raise UploadValidationError(
            "The validated CSV could not be saved in the workspace."
        ) from None
    return UploadedDataset(
        path=target,
        relative_path=relative_path.as_posix(),
        frame=frame,
        byte_count=len(content),
    )


def _safe_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", basename).strip("._")
    return cleaned[:100] or "dataset.csv"
