from __future__ import annotations

import asyncio

import pytest

from agent_tool_runtime import (
    EventType,
    ExecutionContext,
    MemoryEventSink,
    RetryPolicy,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    ToolStatus,
)
from agent_tool_runtime.idempotency import IdempotencyStoreError

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


def build_executor(handler, **tool_overrides):
    registry = ToolRegistry()
    definition = ToolDefinition(
        name="double",
        handler=handler,
        input_schema=SCHEMA,
        **tool_overrides,
    )
    registry.register(definition)
    sink = MemoryEventSink()
    return ToolExecutor(registry, event_sink=sink), sink


@pytest.mark.asyncio
async def test_successful_call_emits_metadata_only_lifecycle_events():
    executor, sink = build_executor(lambda args, context: args["value"] * 2)

    result = await executor.execute(
        ToolCall("call-1", "double", {"value": 4}),
        ExecutionContext("user-7", trace_id="trace-9"),
    )

    assert result.status is ToolStatus.SUCCESS
    assert result.output == 8
    assert result.attempts == 1
    assert [event.event_type for event in sink.events] == [
        EventType.RECEIVED,
        EventType.VALIDATED,
        EventType.AUTHORIZED,
        EventType.ATTEMPT_STARTED,
        EventType.COMPLETED,
    ]
    assert all("value" not in event.attributes for event in sink.events)


@pytest.mark.asyncio
async def test_schema_validation_rejects_call_before_handler():
    called = False

    def handler(args, context):
        nonlocal called
        called = True

    executor, _ = build_executor(handler)
    result = await executor.execute(
        ToolCall("call-2", "double", {"value": "four"}),
        ExecutionContext("user-7"),
    )

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert result.error.code == "schema_validation_failed"
    assert called is False


@pytest.mark.asyncio
async def test_required_scope_is_enforced():
    executor, _ = build_executor(
        lambda args, context: args["value"] * 2,
        required_scopes=frozenset({"tools:execute"}),
    )

    result = await executor.execute(
        ToolCall("call-3", "double", {"value": 4}),
        ExecutionContext("user-7", scopes=frozenset({"tools:read"})),
    )

    assert result.status is ToolStatus.FORBIDDEN
    assert result.error.code == "missing_scope"


@pytest.mark.asyncio
async def test_retryable_failure_succeeds_on_second_attempt():
    class TransientFailure(Exception):
        pass

    attempts = 0

    def handler(args, context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientFailure
        return args["value"] * 2

    executor, sink = build_executor(
        handler,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_ms=0),
        retryable_exceptions=(TransientFailure,),
    )

    result = await executor.execute(
        ToolCall("call-4", "double", {"value": 4}),
        ExecutionContext("user-7"),
    )

    assert result.status is ToolStatus.SUCCESS
    assert result.attempts == 2
    assert attempts == 2
    assert EventType.RETRY_SCHEDULED in [event.event_type for event in sink.events]


@pytest.mark.asyncio
async def test_timeout_is_retried_and_translated():
    attempts = 0

    async def handler(args, context):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.03)

    executor, _ = build_executor(
        handler,
        timeout_seconds=0.005,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_ms=0),
    )

    result = await executor.execute(
        ToolCall("call-5", "double", {"value": 4}),
        ExecutionContext("user-7"),
    )

    assert result.status is ToolStatus.TIMED_OUT
    assert result.attempts == 2
    assert attempts == 2


@pytest.mark.asyncio
async def test_duplicate_call_replays_result_without_running_handler_again():
    calls = 0

    def handler(args, context):
        nonlocal calls
        calls += 1
        return args["value"] * 2

    executor, _ = build_executor(handler)
    call = ToolCall("call-6", "double", {"value": 4})
    context = ExecutionContext("user-7")

    first = await executor.execute(call, context)
    replay = await executor.execute(call, context)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.output == 8
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_duplicates_are_single_flighted():
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(args, context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return args["value"] * 2

    executor, _ = build_executor(handler)
    call = ToolCall("call-7", "double", {"value": 4})
    context = ExecutionContext("user-7")

    first_task = asyncio.create_task(executor.execute(call, context))
    await started.wait()
    second_task = asyncio.create_task(executor.execute(call, context))
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert calls == 1
    assert {first.replayed, second.replayed} == {False, True}


@pytest.mark.asyncio
async def test_reusing_call_id_for_different_arguments_is_a_conflict():
    executor, _ = build_executor(lambda args, context: args["value"] * 2)
    context = ExecutionContext("user-7")

    await executor.execute(ToolCall("call-8", "double", {"value": 4}), context)
    conflict = await executor.execute(ToolCall("call-8", "double", {"value": 5}), context)

    assert conflict.status is ToolStatus.IDEMPOTENCY_CONFLICT
    assert conflict.error.code == "call_id_reused"


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error():
    executor = ToolExecutor(ToolRegistry())

    result = await executor.execute(
        ToolCall("call-9", "missing", {}),
        ExecutionContext("user-7"),
    )

    assert result.status is ToolStatus.NOT_FOUND
    assert result.error.code == "tool_not_found"


@pytest.mark.asyncio
async def test_store_failure_prevents_execution_and_returns_structured_error():
    called = False

    def handler(args, context):
        nonlocal called
        called = True

    class UnavailableStore:
        async def claim(self, call_id, fingerprint):
            raise IdempotencyStoreError("offline")

        async def complete(self, call_id, result):
            raise AssertionError("complete must not be called")

    registry = ToolRegistry()
    registry.register(ToolDefinition("double", handler, SCHEMA))
    executor = ToolExecutor(registry, idempotency_store=UnavailableStore())

    result = await executor.execute(
        ToolCall("call-10", "double", {"value": 4}),
        ExecutionContext("user-7"),
    )

    assert result.status is ToolStatus.FAILED
    assert result.error.code == "idempotency_unavailable"
    assert result.error.retryable is True
    assert called is False


@pytest.mark.asyncio
async def test_lost_claim_returns_handler_result_and_emits_degraded_event():
    class LostClaimStore:
        async def claim(self, call_id, fingerprint):
            return True, asyncio.get_running_loop().create_future()

        async def complete(self, call_id, result):
            return False

    registry = ToolRegistry()
    registry.register(ToolDefinition("double", lambda args, context: 8, SCHEMA))
    sink = MemoryEventSink()
    executor = ToolExecutor(
        registry,
        idempotency_store=LostClaimStore(),
        event_sink=sink,
    )

    result = await executor.execute(
        ToolCall("call-11", "double", {"value": 4}),
        ExecutionContext("user-7"),
    )

    assert result.status is ToolStatus.SUCCESS
    assert EventType.IDEMPOTENCY_DEGRADED in [event.event_type for event in sink.events]
