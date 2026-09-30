"""Independent, market-only D1/H4 Brooks context analyst consumers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from condor.brooks.agent_runner import bind_symbol_tools, run_role
from condor.brooks.contracts import MarketContextV1, MarketContextV2
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.market_tools import CandleSource, HTFMarketTools
from condor.brooks.store import BrooksStore

try:
    from condor.brooks.llm_coordination import run_coordinated_role
except ImportError:  # Compatibility while an older checkout lacks the wrapper.
    run_coordinated_role = run_role

log = logging.getLogger(__name__)


@dataclass
class ContextAnalystConsumer:
    """Run the same market-only analyst for D1 or H4 closed bars."""

    agent_key: str
    source: CandleSource
    store: BrooksStore
    events: EventBus
    timeframe: str = "1d"
    user_id: int | None = None
    timeout_sec: float = 300
    backend_key: str | None = None
    max_role_attempts: int = 2
    retry_backoff_sec: float = 5
    _identity_locks: dict[tuple[str, str, int], asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _identity_lock_users: dict[tuple[str, str, int], int] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @property
    def canonical_timeframe(self) -> str:
        canonical = {"1d": "1d", "d1": "1d", "4h": "4h", "h4": "4h"}.get(
            self.timeframe.strip().lower()
        )
        if canonical is None:
            raise ValueError("ContextAnalystConsumer timeframe must be 1d or 4h")
        return canonical

    @property
    def event_type(self) -> EventType:
        return EventType.D1_BAR_CLOSED if self.canonical_timeframe == "1d" else EventType.H4_BAR_CLOSED

    @property
    def context_timeframe(self) -> str:
        return "D1" if self.canonical_timeframe == "1d" else "H4"

    async def handle(self, event: BrooksEvent) -> MarketContextV2:
        if event.type != self.event_type:
            raise ValueError(f"Context Analyst only handles {self.event_type.value}")
        decision_time_ms = event.payload.get("decision_time_ms")
        if (
            isinstance(decision_time_ms, bool)
            or not isinstance(decision_time_ms, int)
            or decision_time_ms < 0
        ):
            raise ValueError("D1 event requires nonnegative decision_time_ms")
        timeframe = self.canonical_timeframe
        identity = (event.symbol, timeframe, decision_time_ms)
        lock = self._identity_locks.get(identity)
        if lock is None:
            lock = asyncio.Lock()
            self._identity_locks[identity] = lock
        self._identity_lock_users[identity] = (
            self._identity_lock_users.get(identity, 0) + 1
        )
        try:
            async with lock:
                return await self._handle_locked(event, timeframe, decision_time_ms)
        finally:
            remaining = self._identity_lock_users[identity] - 1
            if remaining:
                self._identity_lock_users[identity] = remaining
            else:
                self._identity_lock_users.pop(identity, None)
                self._identity_locks.pop(identity, None)

    async def _handle_locked(
        self, event: BrooksEvent, timeframe: str, decision_time_ms: int
    ) -> MarketContextV2:
        cached_raw = self.store.read_market_context(
            timeframe, symbol=event.symbol
        )
        if cached_raw is not None:
            try:
                cached = MarketContextV2.model_validate(cached_raw)
            except ValidationError:
                cached = None
            if (
                cached is not None
                and cached.symbol == event.symbol
                and cached.timeframe == self.context_timeframe
                and cached.decision_time_ms == decision_time_ms
                and cached.window_bars == 120
            ):
                return cached

        market_tools = HTFMarketTools(
            source=self.source, decision_time_ms=decision_time_ms
        )
        bars = await market_tools.get_closed_candles(event.symbol, timeframe, 120)
        if bars[-1]["close_time_ms"] != decision_time_ms:
            raise ValueError(f"{self.context_timeframe} close event lacks its closed decision bar")
        context: dict[str, Any] = {
            "schema": "brooks.context-analyst-input.v2",
            "role": "CONTEXT_ANALYST",
            "symbol": event.symbol,
            "timeframe": timeframe,
            "decision_time_ms": decision_time_ms,
            "window_bars": len(bars),
            "bars": bars,
        }
        tools = bind_symbol_tools(
            event.symbol,
            {
                "get_closed_candles": market_tools.get_closed_candles,
                "get_recent_structure": market_tools.get_recent_structure,
                "get_volatility": market_tools.get_volatility,
            },
        )
        tool_audit: list[dict[str, Any]] = []
        try:
            result = await run_coordinated_role(
                "CONTEXT_ANALYST",
                agent_key=self.agent_key,
                prompt=context,
                output_model=MarketContextV2,
                market_tools=tools,
                backend_key=self.backend_key,
                priority=10,
                timeout_sec=self.timeout_sec,
                max_role_attempts=self.max_role_attempts,
                retry_backoff_sec=self.retry_backoff_sec,
                tool_audit=tool_audit,
                user_id=self.user_id,
            )
        finally:
            for call in tool_audit:
                if call.get("tool") != "read_brooks_reference":
                    continue
                arguments = call.get("arguments")
                resource = arguments.get("resource") if isinstance(arguments, dict) else None
                try:
                    await self.events.publish(
                        BrooksEvent(
                            type=EventType.BROOKS_REFERENCE_READ,
                            symbol=event.symbol,
                            correlation_id=event.correlation_id,
                            causation_id=event.event_id,
                            payload={
                                "role": "CONTEXT_ANALYST",
                                "timeframe": self.context_timeframe,
                                "resource": resource,
                                "status": call.get("status"),
                                "result_count": call.get("result_count"),
                            },
                        )
                    )
                except Exception:
                    log.exception("Failed to persist Brooks reference-read audit")
        if (
            result.symbol != event.symbol
            or result.decision_time_ms != decision_time_ms
            or result.timeframe != self.context_timeframe
            or result.window_bars != 120
        ):
            raise ValueError("Context Analyst output symbol, time, timeframe or window mismatch")
        self.store.save_market_context(result)
        reference_audit = [
            {
                "resource": (
                    call.get("arguments", {}).get("resource")
                    if isinstance(call.get("arguments"), dict)
                    else None
                ),
                "status": call.get("status"),
            }
            for call in tool_audit
            if call.get("tool") == "read_brooks_reference"
        ]
        await self.events.publish(
            BrooksEvent(
                type=EventType.MARKET_CONTEXT_UPDATED,
                symbol=event.symbol,
                correlation_id=event.correlation_id or str(uuid4()),
                causation_id=event.event_id,
                payload={
                    "timeframe": result.timeframe,
                    "market_context": result.model_dump(mode="json"),
                    "reference_tool_audit": reference_audit,
                },
            )
        )
        return result

    async def run(self) -> None:
        queue = self.events.subscribe([self.event_type])
        try:
            while True:
                event = await queue.get()
                try:
                    await self.handle(event)
                except Exception:
                    log.exception(
                        "Brooks %s Context Analyst failed for %s event %s",
                        self.context_timeframe,
                        event.symbol,
                        event.event_id,
                    )
        finally:
            self.events.unsubscribe(queue)


@dataclass
class HTFAnalystConsumer:
    """Legacy V1 D1 consumer retained for callers that still read htf/latest."""

    agent_key: str
    source: CandleSource
    store: BrooksStore
    events: EventBus
    user_id: int | None = None
    timeout_sec: float = 180

    async def handle(self, event: BrooksEvent) -> MarketContextV1:
        if event.type != EventType.D1_BAR_CLOSED:
            raise ValueError("HTF Analyst only handles D1_BAR_CLOSED")
        decision_time_ms = event.payload.get("decision_time_ms")
        if (
            isinstance(decision_time_ms, bool)
            or not isinstance(decision_time_ms, int)
            or decision_time_ms < 0
        ):
            raise ValueError("D1 event requires nonnegative decision_time_ms")
        market_tools = HTFMarketTools(source=self.source, decision_time_ms=decision_time_ms)
        bars = await market_tools.get_closed_candles(event.symbol, "1d", 120)
        if bars[-1]["close_time_ms"] not in {decision_time_ms, decision_time_ms - 1}:
            raise ValueError("D1 close event lacks its closed decision bar")
        context: dict[str, Any] = {
            "schema": "brooks.htf-market-input.v1",
            "role": "HTF_ANALYST",
            "symbol": event.symbol,
            "decision_time_ms": decision_time_ms,
            "timeframe": "1d",
            "bars": bars,
        }
        result = await run_role(
            "HTF_ANALYST",
            agent_key=self.agent_key,
            prompt=context,
            output_model=MarketContextV1,
            market_tools=bind_symbol_tools(
                event.symbol,
                {
                    "get_closed_candles": market_tools.get_closed_candles,
                    "get_recent_structure": market_tools.get_recent_structure,
                    "get_volatility": market_tools.get_volatility,
                },
            ),
            timeout_sec=self.timeout_sec,
            user_id=self.user_id,
        )
        if (
            result.symbol != event.symbol
            or result.decision_time_ms != decision_time_ms
            or result.timeframe not in {"D1", "1d"}
        ):
            raise ValueError("HTF output symbol, time or timeframe mismatch")
        self.store.save_market_context(result.model_dump())
        await self.events.publish(
            BrooksEvent(
                type=EventType.MARKET_CONTEXT_UPDATED,
                symbol=event.symbol,
                correlation_id=event.correlation_id or str(uuid4()),
                causation_id=event.event_id,
                payload={"market_context": result.model_dump()},
            )
        )
        return result

    async def run(self) -> None:
        queue = self.events.subscribe([EventType.D1_BAR_CLOSED])
        try:
            while True:
                event = await queue.get()
                try:
                    await self.handle(event)
                except Exception:
                    log.exception(
                        "Brooks HTF Analyst failed for %s event %s",
                        event.symbol,
                        event.event_id,
                    )
        finally:
            self.events.unsubscribe(queue)
