"""Public data contracts for the tool runtime."""

from __future__ import annotations

import math
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


class RetryJitter(StrEnum):
    NONE = "none"
    FULL = "full"
    EQUAL = "equal"


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
    jitter: RetryJitter = RetryJitter.NONE

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not math.isfinite(self.base_delay_ms) or not math.isfinite(self.max_delay_ms):
            raise ValueError("retry delays must be finite")
        if self.base_delay_ms < 0 or self.max_delay_ms < 0:
            raise ValueError("retry delays must not be negative")
        if not isinstance(self.jitter, RetryJitter):
            raise ValueError("jitter must be a RetryJitter value")

    def delay_seconds(
        self,
        completed_attempts: int,
        random_source: Callable[[], float] | None = None,
    ) -> float:
        if completed_attempts < 1:
            raise ValueError("completed_attempts must be at least 1")

        capped_ms = self._capped_delay_ms(completed_attempts)
        if self.jitter is RetryJitter.NONE or capped_ms == 0:
            return capped_ms / 1_000
        if random_source is None:
            raise ValueError("a random source is required when jitter is enabled")

        sample = random_source()
        if not math.isfinite(sample) or not 0 <= sample <= 1:
            raise ValueError("random source must return a finite value between 0 and 1")
        if self.jitter is RetryJitter.FULL:
            return capped_ms * sample / 1_000
        if self.jitter is RetryJitter.EQUAL:
            return capped_ms * (0.5 + sample * 0.5) / 1_000
        raise ValueError(f"unsupported jitter strategy: {self.jitter}")

    def _capped_delay_ms(self, completed_attempts: int) -> float:
        if self.base_delay_ms == 0 or self.max_delay_ms == 0:
            return 0.0
        if self.base_delay_ms >= self.max_delay_ms:
            return self.max_delay_ms

        exponent = completed_attempts - 1
        doublings_to_cap = math.ceil(
            math.log2(self.max_delay_ms) - math.log2(self.base_delay_ms)
        )
        if exponent >= doublings_to_cap:
            return self.max_delay_ms
        return min(math.ldexp(self.base_delay_ms, exponent), self.max_delay_ms)


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
