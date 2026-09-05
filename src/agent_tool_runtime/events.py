"""Metadata-only execution events suitable for logs, traces, and metrics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class EventType(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    AUTHORIZED = "authorized"
    ATTEMPT_STARTED = "attempt_started"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    event_type: EventType
    call_id: str
    tool_name: str
    trace_id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


class EventSink(Protocol):
    async def emit(self, event: ExecutionEvent) -> None: ...


class NullEventSink:
    async def emit(self, event: ExecutionEvent) -> None:
        del event


class MemoryEventSink:
    """Small test/demo sink; production adapters can export to OTel or a log stream."""

    def __init__(self) -> None:
        self._events: list[ExecutionEvent] = []
        self._lock = asyncio.Lock()

    @property
    def events(self) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events)

    async def emit(self, event: ExecutionEvent) -> None:
        async with self._lock:
            self._events.append(event)
