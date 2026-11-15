"""Validated application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for local model connectivity and local workflow data."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:1b"
    ollama_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    ollama_fallback_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_path: Path = Path("data/agentflow.sqlite3")
    workspace_path: Path = Path("workspace")

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in the Ollama URL")
        return normalized

    @field_validator("ollama_model")
    @classmethod
    def validate_ollama_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings; clear the cache in tests after changing env vars."""
    return Settings()
