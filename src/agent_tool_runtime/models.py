"""Public data contracts for the tool runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolStatus(StrEnum):
    SUCCESS = "success"
    INVALID_ARGUMENTS = "invalid_arguments"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolStatus
    output: Any = None
    error: ToolError | None = None
    attempts: int = 0
    duration_ms: float = 0.0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("call_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("tool_name must not be empty")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    principal: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.principal.strip():
            raise ValueError("principal must not be empty")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_ms: float = 25.0
    max_delay_ms: float = 1_000.0
    retry_on_timeout: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_ms < 0 or self.max_delay_ms < 0:
            raise ValueError("retry delays must not be negative")

    def delay_seconds(self, completed_attempts: int) -> float:
        delay_ms = self.base_delay_ms * (2 ** max(0, completed_attempts - 1))
        return min(delay_ms, self.max_delay_ms) / 1_000


ToolHandler = Callable[[Mapping[str, Any], ExecutionContext], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    handler: ToolHandler
    input_schema: Mapping[str, Any]
    required_scopes: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 5.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    retryable_exceptions: tuple[type[Exception], ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
