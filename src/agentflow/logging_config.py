"""Structured standard-library logging with basic secret redaction."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|password|secret|authorization)\b\s*[:=]\s*)"
    r"([^\s,;]+)"
)


def _redact(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1[REDACTED]", value)


class RedactingFilter(logging.Filter):
    """Redact common credential assignments from log messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact(record.getMessage())
            record.args = ()
        except (TypeError, ValueError):
            record.msg = "[unformattable log message redacted]"
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger for structured console output."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())
