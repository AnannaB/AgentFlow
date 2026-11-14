"""Typed tool metadata, input/output contracts, and callable definition."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentflow.tools.context import ToolExecutionContext


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolContract(BaseModel):
    """Strict base for all tool input and output schemas."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolInput(ToolContract):
    """Base input contract for tools."""


class ToolOutput(ToolContract):
    """Base output contract for tools."""


class ToolMetadata(ToolContract):
    """Discoverable properties and safety/retry policy for one tool version."""

    name: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=2000)
    risk: ToolRisk = ToolRisk.LOW
    timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    retryable: bool = False
    idempotent: bool = True
    supports_idempotency_key: bool = False
    requires_approval: bool = False

    @model_validator(mode="after")
    def retries_must_be_safe(self) -> "ToolMetadata":
        if self.retryable and not (self.idempotent or self.supports_idempotency_key):
            raise ValueError("retryable tools must be idempotent or accept an idempotency key")
        return self


@dataclass(frozen=True, slots=True)
class ToolDefinition[InputT: ToolInput, OutputT: ToolOutput]:
    """A tool contract and its explicitly registered Python implementation."""

    metadata: ToolMetadata
    input_model: type[InputT]
    output_model: type[OutputT]
    handler: Callable[[InputT, ToolExecutionContext], OutputT]

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ToolMetadata):
            raise TypeError("metadata must be a ToolMetadata instance")
        if not isinstance(self.input_model, type) or not issubclass(self.input_model, ToolInput):
            raise TypeError("input_model must extend ToolInput")
        if not isinstance(self.output_model, type) or not issubclass(self.output_model, ToolOutput):
            raise TypeError("output_model must extend ToolOutput")
        if not callable(self.handler):
            raise TypeError("handler must be callable")
