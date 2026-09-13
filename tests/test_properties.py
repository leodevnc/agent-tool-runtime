from __future__ import annotations

import asyncio
import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from agent_tool_runtime import (
    ExecutionContext,
    RetryJitter,
    RetryPolicy,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    ToolStatus,
)

FINITE_DELAY = st.floats(
    min_value=0,
    max_value=1e12,
    allow_nan=False,
    allow_infinity=False,
)


@given(
    base_delay_ms=FINITE_DELAY,
    max_delay_ms=FINITE_DELAY,
    completed_attempts=st.integers(min_value=1, max_value=10_000),
    sample=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
    jitter=st.sampled_from([RetryJitter.FULL, RetryJitter.EQUAL]),
)
def test_jittered_delay_stays_inside_its_strategy_bounds(
    base_delay_ms,
    max_delay_ms,
    completed_attempts,
    sample,
    jitter,
):
    policy = RetryPolicy(
        max_attempts=2,
        base_delay_ms=base_delay_ms,
        max_delay_ms=max_delay_ms,
        jitter=jitter,
    )
    ceiling = policy.delay_seconds(completed_attempts, lambda: 1.0)
    delay = policy.delay_seconds(completed_attempts, lambda: sample)

    assert 0 <= delay <= ceiling <= max_delay_ms / 1_000
    if jitter is RetryJitter.EQUAL:
        assert delay >= ceiling / 2


@given(
    base_delay_ms=FINITE_DELAY,
    max_delay_ms=FINITE_DELAY,
    completed_attempts=st.integers(min_value=1, max_value=10_000),
)
def test_unjittered_delay_is_finite_and_capped(
    base_delay_ms,
    max_delay_ms,
    completed_attempts,
):
    policy = RetryPolicy(
        max_attempts=2,
        base_delay_ms=base_delay_ms,
        max_delay_ms=max_delay_ms,
    )

    delay = policy.delay_seconds(completed_attempts)

    assert 0 <= delay <= max_delay_ms / 1_000


@pytest.mark.parametrize("sample", [-0.01, 1.01, math.inf, math.nan])
def test_jitter_rejects_invalid_random_samples(sample):
    policy = RetryPolicy(max_attempts=2, jitter=RetryJitter.FULL)

    with pytest.raises(ValueError, match="random source"):
        policy.delay_seconds(1, lambda: sample)


def test_jitter_requires_a_random_source():
    policy = RetryPolicy(max_attempts=2, jitter=RetryJitter.FULL)

    with pytest.raises(ValueError, match="random source"):
        policy.delay_seconds(1)


@settings(deadline=None, max_examples=50)
@given(value=st.integers(), repeats=st.integers(min_value=2, max_value=12))
def test_duplicate_calls_execute_handler_once(value, repeats):
    async def scenario():
        calls = 0

        def handler(arguments, context):
            nonlocal calls
            calls += 1
            return arguments["value"] * 2

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "double",
                handler,
                {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            )
        )
        executor = ToolExecutor(registry)
        call = ToolCall("property-call", "double", {"value": value})
        context = ExecutionContext("property-user")

        results = [await executor.execute(call, context) for _ in range(repeats)]

        assert calls == 1
        assert results[0].replayed is False
        assert all(result.output == value * 2 for result in results)
        assert all(result.replayed for result in results[1:])

    asyncio.run(scenario())


@settings(deadline=None, max_examples=50)
@given(first=st.integers(), second=st.integers())
def test_call_id_reuse_never_executes_different_arguments(first, second):
    assume(first != second)

    async def scenario():
        observed = []

        def handler(arguments, context):
            observed.append(arguments["value"])
            return arguments["value"]

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "identity",
                handler,
                {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            )
        )
        executor = ToolExecutor(registry)
        context = ExecutionContext("property-user")

        initial = await executor.execute(
            ToolCall("reused-call", "identity", {"value": first}),
            context,
        )
        conflict = await executor.execute(
            ToolCall("reused-call", "identity", {"value": second}),
            context,
        )

        assert initial.status is ToolStatus.SUCCESS
        assert conflict.status is ToolStatus.IDEMPOTENCY_CONFLICT
        assert observed == [first]

    asyncio.run(scenario())
