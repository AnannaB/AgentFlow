"""Output persistence bound and UTF-8 byte-count tests."""

import json

import pytest
from pydantic import ValidationError

from agentflow.domain.models import TOOL_RESULT_MAX_BYTES, ToolResult


def test_tool_result_data_accepts_exact_256_kib_and_rejects_one_byte_over():
    overhead = len(
        json.dumps({"payload": ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    exact = "x" * (TOOL_RESULT_MAX_BYTES - overhead)
    ToolResult(success=True, data={"payload": exact}, summary="At limit")
    with pytest.raises(ValidationError, match="262144-byte limit"):
        ToolResult(success=True, data={"payload": exact + "x"}, summary="Over limit")


def test_tool_result_limit_counts_utf8_bytes_not_characters():
    overhead = len(
        json.dumps({"payload": ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    exact_pairs = (TOOL_RESULT_MAX_BYTES - overhead) // 2
    payload = "é" * exact_pairs
    ToolResult(success=True, data={"payload": payload}, summary="UTF-8 boundary")
    with pytest.raises(ValidationError, match="262144-byte limit"):
        ToolResult(success=True, data={"payload": payload + "é"}, summary="UTF-8 over limit")
