from __future__ import annotations

import asyncio
import json

import pytest

from agent_tool_runtime import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolStatus,
)
from agent_tool_runtime.idempotency import IdempotencyConflictError
from agent_tool_runtime.redis_idempotency import (
    CLAIM_SCRIPT,
    COMPLETE_SCRIPT,
    RedisIdempotencyStore,
)


class FakeRedis:
    """A deterministic script-level fake; it models TTL but does not interpret Lua."""

    def __init__(self, clock):
        self.clock = clock
        self.records: dict[str, tuple[str, float]] = {}

    async def eval(self, script, numkeys, *values):
        assert numkeys == 1
        key, *args = values
        self._expire(key)
        if script == CLAIM_SCRIPT:
            return self._claim(key, *args)
        if script == COMPLETE_SCRIPT:
            return self._complete(key, *args)
        raise AssertionError("unexpected script")

    def _claim(self, key, fingerprint, token, pending, lease_ms):
        del token
        current = self.records.get(key)
        if current is None:
            self.records[key] = (pending, self.clock() + lease_ms / 1_000)
            return [b"OWNER", pending.encode()]
        payload = current[0]
        record = json.loads(payload)
        if record["fingerprint"] != fingerprint:
            return [b"CONFLICT", payload.encode()]
        if record["state"] == "completed":
            return [b"REPLAY", payload.encode()]
        return [b"PENDING", payload.encode()]

    def _complete(self, key, fingerprint, token, completed, ttl_ms):
        current = self.records.get(key)
        if current is None:
            return 0
        record = json.loads(current[0])
        if (
            record["state"] != "pending"
            or record["fingerprint"] != fingerprint
            or record["token"] != token
        ):
            return 0
        self.records[key] = (completed, self.clock() + ttl_ms / 1_000)
        return 1

    def _expire(self, key):
        current = self.records.get(key)
        if current is not None and current[1] <= self.clock():
            del self.records[key]


def result(call_id="call-1"):
    return ToolResult(
        call_id=call_id,
        tool_name="double",
        status=ToolStatus.SUCCESS,
        output={"value": 8},
        attempts=1,
        duration_ms=4.2,
    )


@pytest.mark.asyncio
async def test_completed_result_is_replayed_across_store_instances():
    now = [100.0]
    redis = FakeRedis(lambda: now[0])
    owner_store = RedisIdempotencyStore(redis, clock=lambda: now[0])
    follower_store = RedisIdempotencyStore(redis, clock=lambda: now[0])

    owner, _ = await owner_store.claim("call-1", "fingerprint-a")
    stored = await owner_store.complete("call-1", result())
    follower, completed = await follower_store.claim("call-1", "fingerprint-a")

    assert owner is True
    assert stored is True
    assert follower is False
    assert (await completed).output == {"value": 8}


@pytest.mark.asyncio
async def test_same_call_id_with_different_fingerprint_conflicts():
    now = [100.0]
    redis = FakeRedis(lambda: now[0])
    first = RedisIdempotencyStore(redis, clock=lambda: now[0])
    second = RedisIdempotencyStore(redis, clock=lambda: now[0])

    await first.claim("call-1", "fingerprint-a")

    with pytest.raises(IdempotencyConflictError):
        await second.claim("call-1", "fingerprint-b")


@pytest.mark.asyncio
async def test_expired_owner_lease_can_be_reacquired_with_fencing():
    now = [100.0]
    redis = FakeRedis(lambda: now[0])
    old_owner = RedisIdempotencyStore(redis, lease_seconds=1, clock=lambda: now[0])
    new_owner = RedisIdempotencyStore(redis, lease_seconds=1, clock=lambda: now[0])

    first_claim, _ = await old_owner.claim("call-1", "fingerprint-a")
    now[0] += 1.1
    second_claim, _ = await new_owner.claim("call-1", "fingerprint-a")

    assert first_claim is True
    assert second_claim is True
    assert await old_owner.complete("call-1", result()) is False
    assert await new_owner.complete("call-1", result()) is True


@pytest.mark.asyncio
async def test_follower_polls_until_owner_completes():
    now = [100.0]
    redis = FakeRedis(lambda: now[0])
    owner_store = RedisIdempotencyStore(redis, clock=lambda: now[0])
    follower_store = RedisIdempotencyStore(
        redis,
        poll_interval_seconds=0,
        clock=lambda: now[0],
    )

    await owner_store.claim("call-1", "fingerprint-a")
    follower_task = asyncio.create_task(follower_store.claim("call-1", "fingerprint-a"))
    await asyncio.sleep(0)
    await owner_store.complete("call-1", result())
    owner, completed = await follower_task

    assert owner is False
    assert (await completed).status is ToolStatus.SUCCESS


@pytest.mark.asyncio
async def test_two_executors_share_completed_result_through_redis():
    now = [100.0]
    redis = FakeRedis(lambda: now[0])
    calls = 0

    def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"value": arguments["value"] * 2}

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
    first_executor = ToolExecutor(
        registry,
        idempotency_store=RedisIdempotencyStore(redis, clock=lambda: now[0]),
    )
    second_executor = ToolExecutor(
        registry,
        idempotency_store=RedisIdempotencyStore(redis, clock=lambda: now[0]),
    )
    call = ToolCall("call-shared", "double", {"value": 4})
    context = ExecutionContext("user-7")

    first = await first_executor.execute(call, context)
    replay = await second_executor.execute(call, context)

    assert first.output == {"value": 8}
    assert replay.output == {"value": 8}
    assert replay.replayed is True
    assert calls == 1
