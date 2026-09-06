"""Redis-backed idempotency with atomic leases and durable completed results."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .idempotency import IdempotencyConflictError, IdempotencyStoreError
from .models import ToolError, ToolResult, ToolStatus

CLAIM_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
  local created = redis.call('SET', KEYS[1], ARGV[3], 'PX', ARGV[4], 'NX')
  if created then
    return {'OWNER', ARGV[3]}
  end
  current = redis.call('GET', KEYS[1])
  if not current then
    return {'RETRY', ''}
  end
end

local record = cjson.decode(current)
if record.fingerprint ~= ARGV[1] then
  return {'CONFLICT', current}
end
if record.state == 'completed' then
  return {'REPLAY', current}
end
return {'PENDING', current}
"""

COMPLETE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
  return 0
end

local record = cjson.decode(current)
if record.state ~= 'pending' then
  return 0
end
if record.fingerprint ~= ARGV[1] or record.token ~= ARGV[2] then
  return 0
end

redis.call('SET', KEYS[1], ARGV[3], 'PX', ARGV[4])
return 1
"""

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class AsyncRedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...


class RedisIdempotencyStore:
    """Coordinates duplicate calls across processes using Redis.

    Pending records use a short lease. A worker that observes an active lease polls for a completed
    result. If the owner disappears, Redis expires the lease and a waiting worker can acquire a new
    fencing token. Completion succeeds only for the current token.
    """

    def __init__(
        self,
        client: AsyncRedisClient,
        *,
        key_prefix: str = "agent-tool-runtime:idempotency:",
        lease_seconds: float = 30.0,
        result_ttl_seconds: float = 86_400.0,
        wait_timeout_seconds: float = 35.0,
        poll_interval_seconds: float = 0.05,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        if lease_seconds <= 0 or result_ttl_seconds <= 0 or wait_timeout_seconds <= 0:
            raise ValueError("lease, result TTL, and wait timeout must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll interval must not be negative")
        self._client = client
        self._key_prefix = key_prefix
        self._lease_ms = max(1, int(lease_seconds * 1_000))
        self._result_ttl_ms = max(1, int(result_ttl_seconds * 1_000))
        self._wait_timeout_seconds = wait_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._owned: dict[str, tuple[str, str, asyncio.Future[ToolResult]]] = {}

    async def claim(
        self,
        call_id: str,
        fingerprint: str,
    ) -> tuple[bool, asyncio.Future[ToolResult]]:
        deadline = self._clock() + self._wait_timeout_seconds
        token = uuid.uuid4().hex
        key = self._key(call_id)
        pending = self._encode_pending(fingerprint, token)

        while True:
            try:
                response = await self._client.eval(
                    CLAIM_SCRIPT,
                    1,
                    key,
                    fingerprint,
                    token,
                    pending,
                    self._lease_ms,
                )
                state, payload = self._decode_response(response)
            except IdempotencyStoreError:
                raise
            except Exception as exc:
                raise IdempotencyStoreError("redis claim failed") from exc

            if state == "OWNER":
                future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
                self._owned[call_id] = (fingerprint, token, future)
                return True, future
            if state == "CONFLICT":
                raise IdempotencyConflictError(call_id)
            if state == "REPLAY":
                future = asyncio.get_running_loop().create_future()
                future.set_result(self._decode_completed(payload, fingerprint))
                return False, future
            if state == "RETRY":
                continue
            if state != "PENDING":
                raise IdempotencyStoreError(f"unknown redis claim state: {state}")
            if self._clock() >= deadline:
                raise IdempotencyStoreError("timed out waiting for the idempotency owner")
            await self._sleep(self._poll_interval_seconds)

    async def complete(self, call_id: str, result: ToolResult) -> bool:
        ownership = self._owned.pop(call_id, None)
        if ownership is None:
            raise IdempotencyStoreError("completion attempted without an owned claim")
        fingerprint, token, future = ownership
        completed = self._encode_completed(fingerprint, result)

        try:
            stored = await self._client.eval(
                COMPLETE_SCRIPT,
                1,
                self._key(call_id),
                fingerprint,
                token,
                completed,
                self._result_ttl_ms,
            )
        except Exception as exc:
            if not future.done():
                future.set_result(result)
            raise IdempotencyStoreError("redis completion failed") from exc

        if not future.done():
            future.set_result(result)
        return bool(stored)

    def _key(self, call_id: str) -> str:
        return f"{self._key_prefix}{call_id}"

    @staticmethod
    def _encode_pending(fingerprint: str, token: str) -> str:
        return json.dumps(
            {"v": 1, "state": "pending", "fingerprint": fingerprint, "token": token},
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _encode_completed(fingerprint: str, result: ToolResult) -> str:
        try:
            return json.dumps(
                {
                    "v": 1,
                    "state": "completed",
                    "fingerprint": fingerprint,
                    "result": {
                        "call_id": result.call_id,
                        "tool_name": result.tool_name,
                        "status": result.status.value,
                        "output": result.output,
                        "error": None
                        if result.error is None
                        else {
                            "code": result.error.code,
                            "message": result.error.message,
                            "retryable": result.error.retryable,
                        },
                        "attempts": result.attempts,
                        "duration_ms": result.duration_ms,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise IdempotencyStoreError("tool result is not JSON serializable") from exc

    @staticmethod
    def _decode_response(response: Any) -> tuple[str, str]:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            raise IdempotencyStoreError("malformed redis claim response")
        state = RedisIdempotencyStore._text(response[0])
        payload = RedisIdempotencyStore._text(response[1])
        return state, payload

    @staticmethod
    def _decode_completed(payload: str, expected_fingerprint: str) -> ToolResult:
        try:
            record = json.loads(payload)
            if record["state"] != "completed" or record["fingerprint"] != expected_fingerprint:
                raise ValueError("completed record does not match claim")
            raw = record["result"]
            raw_error = raw["error"]
            error = None if raw_error is None else ToolError(**raw_error)
            return ToolResult(
                call_id=raw["call_id"],
                tool_name=raw["tool_name"],
                status=ToolStatus(raw["status"]),
                output=raw["output"],
                error=error,
                attempts=raw["attempts"],
                duration_ms=raw["duration_ms"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IdempotencyStoreError("malformed completed result in Redis") from exc

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode()
        if isinstance(value, str):
            return value
        raise IdempotencyStoreError("redis response must contain text values")
