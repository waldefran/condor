"""D1 Brooks HTF Analyst: independent market context and event."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from uuid import uuid4

from condor.brooks.agent_runner import bind_symbol_tools, run_role
from condor.brooks.contracts import MarketContextV1
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.market_tools import CandleSource, HTFMarketTools
from condor.brooks.store import BrooksStore

log = logging.getLogger(__name__)


@dataclass
class HTFAnalystConsumer:
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
        if isinstance(decision_time_ms, bool) or not isinstance(decision_time_ms, int) or decision_time_ms < 0:
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
            market_tools=bind_symbol_tools(event.symbol, {
                "get_closed_candles": market_tools.get_closed_candles,
                "get_recent_structure": market_tools.get_recent_structure,
                "get_volatility": market_tools.get_volatility,
            }),
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
        await self.events.publish(BrooksEvent(
            type=EventType.MARKET_CONTEXT_UPDATED,
            symbol=event.symbol,
            correlation_id=event.correlation_id or str(uuid4()),
            causation_id=event.event_id,
            payload={"market_context": result.model_dump()},
        ))
        return result

    async def run(self) -> None:
        queue = self.events.subscribe([EventType.D1_BAR_CLOSED])
        try:
            while True:
                event = await queue.get()
                try:
                    await self.handle(event)
                except Exception:
                    log.exception("Brooks HTF Analyst failed for %s event %s", event.symbol, event.event_id)
        finally:
            self.events.unsubscribe(queue)
