"""The policy and reliability boundary around agent-selected tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from jsonschema import Draft7Validator

from .events import EventSink, EventType, ExecutionEvent, NullEventSink
from .idempotency import (
    IdempotencyConflictError,
    IdempotencyStore,
    IdempotencyStoreError,
    InMemoryIdempotencyStore,
)
from .models import ExecutionContext, ToolCall, ToolDefinition, ToolError, ToolResult, ToolStatus
from .registry import ToolRegistry

Sleep = Callable[[float], Awaitable[None]]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        idempotency_store: IdempotencyStore | None = None,
        event_sink: EventSink | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._idempotency = idempotency_store or InMemoryIdempotencyStore()
        self._events = event_sink or NullEventSink()
        self._sleep = sleep

    async def execute(self, call: ToolCall, context: ExecutionContext) -> ToolResult:
        started = time.monotonic()
        await self._emit(EventType.RECEIVED, call, context)

        try:
            fingerprint = self._fingerprint(call, context)
        except (TypeError, ValueError):
            return await self._finish(
                call,
                context,
                ToolStatus.INVALID_ARGUMENTS,
                started,
                error=ToolError("arguments_not_json", "arguments must be valid JSON values"),
            )

        try:
            owner, future = await self._idempotency.claim(call.call_id, fingerprint)
        except IdempotencyConflictError:
            return await self._finish(
                call,
                context,
                ToolStatus.IDEMPOTENCY_CONFLICT,
                started,
                error=ToolError(
                    "call_id_reused",
                    "call_id was already used with a different request",
                ),
            )
        except IdempotencyStoreError:
            return await self._finish(
                call,
                context,
                ToolStatus.FAILED,
                started,
                error=ToolError(
                    "idempotency_unavailable",
                    "tool was not executed because idempotency state is unavailable",
                    True,
                ),
            )

        if not owner:
            cached = await asyncio.shield(future)
            replay = replace(cached, replayed=True)
            await self._emit(EventType.REPLAYED, call, context, status=cached.status.value)
            return replay

        result = await self._execute_owned(call, context, started)
        try:
            persisted = await self._idempotency.complete(call.call_id, result)
            if not persisted:
                await self._emit(
                    EventType.IDEMPOTENCY_DEGRADED,
                    call,
                    context,
                    reason="claim_lost_before_completion",
                )
        except IdempotencyStoreError:
            await self._emit(
                EventType.IDEMPOTENCY_DEGRADED,
                call,
                context,
                reason="completion_failed",
            )
        return result

    async def _execute_owned(
        self,
        call: ToolCall,
        context: ExecutionContext,
        started: float,
    ) -> ToolResult:
        tool = self._registry.get(call.tool_name)
        if tool is None:
            return await self._finish(
                call,
                context,
                ToolStatus.NOT_FOUND,
                started,
                error=ToolError("tool_not_found", "tool is not registered"),
            )

        validation_error = self._validate(tool, call)
        if validation_error is not None:
            return await self._finish(
                call,
                context,
                ToolStatus.INVALID_ARGUMENTS,
                started,
                error=ToolError("schema_validation_failed", validation_error),
            )
        await self._emit(EventType.VALIDATED, call, context)

        missing_scopes = tool.required_scopes - context.scopes
        if missing_scopes:
            return await self._finish(
                call,
                context,
                ToolStatus.FORBIDDEN,
                started,
                error=ToolError(
                    "missing_scope",
                    f"principal lacks required scopes: {', '.join(sorted(missing_scopes))}",
                ),
            )
        await self._emit(EventType.AUTHORIZED, call, context)

        return await self._run_attempts(tool, call, context, started)

    async def _run_attempts(
        self,
        tool: ToolDefinition,
        call: ToolCall,
        context: ExecutionContext,
        started: float,
    ) -> ToolResult:
        policy = tool.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            await self._emit(EventType.ATTEMPT_STARTED, call, context, attempt=attempt)
            try:
                output = await asyncio.wait_for(
                    self._invoke(tool, call, context),
                    timeout=tool.timeout_seconds,
                )
                return await self._finish(
                    call,
                    context,
                    ToolStatus.SUCCESS,
                    started,
                    output=output,
                    attempts=attempt,
                )
            except TimeoutError:
                retryable = policy.retry_on_timeout and attempt < policy.max_attempts
                if not retryable:
                    return await self._finish(
                        call,
                        context,
                        ToolStatus.TIMED_OUT,
                        started,
                        error=ToolError("tool_timed_out", "tool exceeded its deadline", True),
                        attempts=attempt,
                    )
            except Exception as exc:  # noqa: BLE001 - boundary intentionally translates failures
                retryable = isinstance(exc, tool.retryable_exceptions)
                if not retryable or attempt >= policy.max_attempts:
                    return await self._finish(
                        call,
                        context,
                        ToolStatus.FAILED,
                        started,
                        error=ToolError(
                            "tool_execution_failed",
                            "tool failed without exposing internal exception details",
                            retryable,
                        ),
                        attempts=attempt,
                    )

            delay = policy.delay_seconds(attempt)
            await self._emit(
                EventType.RETRY_SCHEDULED,
                call,
                context,
                attempt=attempt,
                delay_ms=delay * 1_000,
            )
            await self._sleep(delay)

        raise AssertionError("retry loop must return")

    @staticmethod
    async def _invoke(
        tool: ToolDefinition,
        call: ToolCall,
        context: ExecutionContext,
    ) -> Any:
        value = tool.handler(call.arguments, context)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _validate(tool: ToolDefinition, call: ToolCall) -> str | None:
        validator = Draft7Validator(tool.input_schema)
        errors = sorted(
            validator.iter_errors(dict(call.arguments)),
            key=lambda error: list(error.path),
        )
        if not errors:
            return None
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "$"
        return f"{location}: {first.message}"

    @staticmethod
    def _fingerprint(call: ToolCall, context: ExecutionContext) -> str:
        payload = {
            "arguments": call.arguments,
            "principal": context.principal,
            "tool_name": call.tool_name,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    async def _finish(
        self,
        call: ToolCall,
        context: ExecutionContext,
        status: ToolStatus,
        started: float,
        *,
        output: Any = None,
        error: ToolError | None = None,
        attempts: int = 0,
    ) -> ToolResult:
        result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            status=status,
            output=output,
            error=error,
            attempts=attempts,
            duration_ms=(time.monotonic() - started) * 1_000,
        )
        await self._emit(EventType.COMPLETED, call, context, status=status.value, attempts=attempts)
        return result

    async def _emit(
        self,
        event_type: EventType,
        call: ToolCall,
        context: ExecutionContext,
        **attributes: Any,
    ) -> None:
        event = ExecutionEvent(
            event_type=event_type,
            call_id=call.call_id,
            tool_name=call.tool_name,
            trace_id=context.trace_id,
            attributes=attributes,
        )
        try:
            await self._events.emit(event)
        except Exception:  # noqa: BLE001 - telemetry must not break tool execution
            return
