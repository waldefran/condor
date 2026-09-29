"""In-process Brooks events, persisted before delivery to consumers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Iterable
from uuid import uuid4

if TYPE_CHECKING:
    from .store import BrooksStore


class EventType(StrEnum):
    H1_BAR_CLOSED = "H1_BAR_CLOSED"
    H4_BAR_CLOSED = "H4_BAR_CLOSED"
    D1_BAR_CLOSED = "D1_BAR_CLOSED"
    TRADER_INTENT_CREATED = "TRADER_INTENT_CREATED"
    MARKET_CONTEXT_UPDATED = "MARKET_CONTEXT_UPDATED"
    BROOKS_REFERENCE_READ = "BROOKS_REFERENCE_READ"
    PM_TIMER = "PM_TIMER"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CHANGED = "POSITION_CHANGED"
    POSITION_CLOSED = "POSITION_CLOSED"
    ORDER_CHANGED = "ORDER_CHANGED"
    FILL = "FILL"
    MANAGEMENT_INTENT_CREATED = "MANAGEMENT_INTENT_CREATED"
    GM_ENTRY_APPROVED = "GM_ENTRY_APPROVED"
    GM_ENTRY_REJECTED = "GM_ENTRY_REJECTED"
    GM_MANAGEMENT_APPROVED = "GM_MANAGEMENT_APPROVED"
    GM_MANAGEMENT_REJECTED = "GM_MANAGEMENT_REJECTED"
    EXECUTION_SUBMITTED = "EXECUTION_SUBMITTED"
    EXECUTION_CONFIRMED = "EXECUTION_CONFIRMED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    HEDGE_OPENED = "HEDGE_OPENED"
    HEDGE_CHANGED = "HEDGE_CHANGED"
    HEDGE_REMOVED = "HEDGE_REMOVED"
    MARKET_ANALYSIS_COMPLETED = "MARKET_ANALYSIS_COMPLETED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ROLE_RUN_FAILED = "ROLE_RUN_FAILED"
    TRADER_DECISION_FAILED = "TRADER_DECISION_FAILED"


@dataclass(frozen=True)
class BrooksEvent:
    type: EventType
    symbol: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrooksEvent:
        return cls(
            type=EventType(data["type"]),
            symbol=data["symbol"],
            payload=data["payload"],
            correlation_id=data.get("correlation_id"),
            causation_id=data.get("causation_id"),
            event_id=data["event_id"],
            created_at_ms=data["created_at_ms"],
        )


class EventBus:
    """Fan out each event to independent queues after its durable append."""

    def __init__(self, store: BrooksStore):
        self.store = store
        self._default_queue: asyncio.Queue[BrooksEvent] | None = None
        self._subscribers: dict[asyncio.Queue[BrooksEvent], set[EventType] | None] = {}
        self._publish_lock = asyncio.Lock()
        self._closed = False

    @property
    def queue(self) -> asyncio.Queue[BrooksEvent]:
        """Optional single-consumer queue, created only when used."""
        if self._default_queue is None:
            self._default_queue = self.subscribe()
        return self._default_queue

    def subscribe(
        self, types: Iterable[EventType] | None = None
    ) -> asyncio.Queue[BrooksEvent]:
        if self._closed:
            raise RuntimeError("Brooks event bus is closed")
        queue: asyncio.Queue[BrooksEvent] = asyncio.Queue()
        self._subscribers[queue] = set(types) if types is not None else None
        return queue

    def unsubscribe(self, queue: asyncio.Queue[BrooksEvent]) -> None:
        self._subscribers.pop(queue, None)

    async def publish(self, event: BrooksEvent) -> None:
        async with self._publish_lock:
            if self._closed:
                raise RuntimeError("Brooks event bus is closed")
            self.store.append_event(event)
            for queue, types in tuple(self._subscribers.items()):
                if types is None or event.type in types:
                    queue.put_nowait(event)

    def close(self) -> None:
        self._closed = True
        self._subscribers.clear()
