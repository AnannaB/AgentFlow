"""Tests for structured logging and credential redaction."""

import json
import logging

from agentflow.logging_config import JsonFormatter, RedactingFilter


def test_redacting_filter_removes_common_secret_values() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "api_key=%s", ("super-secret",), None
    )

    assert RedactingFilter().filter(record)
    output = JsonFormatter().format(record)

    assert "super-secret" not in output
    assert "[REDACTED]" in output
    assert json.loads(output)["level"] == "INFO"


def test_json_formatter_emits_structured_record() -> None:
    record = logging.LogRecord("agentflow.test", logging.WARNING, __file__, 8, "careful", (), None)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["logger"] == "agentflow.test"
    assert payload["message"] == "careful"
    assert payload["level"] == "WARNING"
