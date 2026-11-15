"""Tests for validated Phase 0 settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentflow.config import Settings


def test_settings_accept_valid_local_ollama_configuration() -> None:
    settings = Settings(
        ollama_base_url="http://localhost:11434/",
        ollama_model="  qwen2.5  ",
        ollama_timeout_seconds=30,
    )

    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "qwen2.5"
    assert settings.ollama_timeout_seconds == 30
    assert settings.ollama_fallback_enabled is True


def test_settings_include_local_database_and_workspace_paths() -> None:
    settings = Settings(
        database_path="data/custom.sqlite3",
        workspace_path="local-workspace",
    )

    assert settings.database_path == Path("data/custom.sqlite3")
    assert settings.workspace_path == Path("local-workspace")


@pytest.mark.parametrize(
    "url",
    ["localhost:11434", "ftp://localhost:11434", "http://user:pass@localhost:11434"],
)
def test_settings_reject_invalid_or_credential_bearing_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(ollama_base_url=url)


@pytest.mark.parametrize("timeout", [0, -1, 601])
def test_settings_bound_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(ollama_timeout_seconds=timeout)
