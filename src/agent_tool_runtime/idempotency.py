"""Single-flight idempotency for retries and duplicate model tool calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .models import ToolResult


class IdempotencyConflictError(Exception):
    """Raised when a call ID is reused for different work."""


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    future: asyncio.Future[ToolResult]


class InMemoryIdempotencyStore:
    """Process-local reference store.

    A durable Redis/Postgres adapter is intentionally left as the next milestone.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def claim(
        self,
        call_id: str,
        fingerprint: str,
    ) -> tuple[bool, asyncio.Future[ToolResult]]:
        async with self._lock:
            existing = self._entries.get(call_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflictError(call_id)
                return False, existing.future

            future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
            self._entries[call_id] = _Entry(fingerprint=fingerprint, future=future)
            return True, future

    async def complete(self, call_id: str, result: ToolResult) -> None:
        async with self._lock:
            entry = self._entries[call_id]
            if not entry.future.done():
                entry.future.set_result(result)
