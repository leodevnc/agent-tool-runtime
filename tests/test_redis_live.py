from __future__ import annotations

import os
import uuid

import pytest

from agent_tool_runtime import RedisIdempotencyStore, ToolResult, ToolStatus
from agent_tool_runtime.idempotency import IdempotencyConflictError

redis_asyncio = pytest.importorskip("redis.asyncio")


@pytest.mark.redis
@pytest.mark.skipif("REDIS_URL" not in os.environ, reason="REDIS_URL is not configured")
@pytest.mark.asyncio
async def test_live_redis_claim_complete_replay_and_conflict():
    client = redis_asyncio.Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    prefix = f"agent-tool-runtime:test:{uuid.uuid4().hex}:"
    first = RedisIdempotencyStore(client, key_prefix=prefix)
    second = RedisIdempotencyStore(client, key_prefix=prefix)
    result = ToolResult(
        call_id="call-live",
        tool_name="double",
        status=ToolStatus.SUCCESS,
        output={"value": 8},
        attempts=1,
        duration_ms=2.5,
    )

    try:
        owner, _ = await first.claim("call-live", "fingerprint-a")
        stored = await first.complete("call-live", result)
        follower, replay = await second.claim("call-live", "fingerprint-a")

        assert owner is True
        assert stored is True
        assert follower is False
        assert (await replay).output == {"value": 8}

        with pytest.raises(IdempotencyConflictError):
            await second.claim("call-live", "fingerprint-b")
    finally:
        await client.delete(f"{prefix}call-live")
        await client.aclose()
