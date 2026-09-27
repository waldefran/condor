"""H1 Brooks Trader: market-only analysis, durable intent, then event."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from uuid import uuid4

from condor.brooks.agent_runner import bind_symbol_tools, run_role
from condor.brooks.contracts import MarketContextV1, TradeIntentV2
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.market_tools import CandleSource, TraderMarketTools
from condor.brooks.store import BrooksStore

log = logging.getLogger(__name__)


def _decision_time(event: BrooksEvent) -> int:
    value = event.payload.get("decision_time_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("H1 event requires nonnegative decision_time_ms")
    return value


def _validate_references(intent: TradeIntentV2, windows: dict[str, dict[str, Any]]) -> None:
    """Entry prices must be exact fields of cited closed input bars."""
    if intent.decision == "NO_TRADE":
        if not intent.setup or not intent.setup.no_trade_reason:
            raise ValueError("NO_TRADE requires a specific no_trade_reason")
        return
    if (
        not intent.setup
        or intent.setup.trigger_status not in {"pending", "triggered", "present"}
        or intent.setup.location_assessment in {"poor", "unknown"}
        or intent.setup.no_trade_reason is not None
    ):
        raise ValueError("entry setup is not actionable")
    if intent.decision_timeframe != "M15":
        raise ValueError("production entry must cite M15 decision timeframe")
    if intent.setup.trigger_status == "pending" and intent.trigger:
        expected_direction = "above" if intent.decision == "ENTER_LONG" else "below"
        if intent.trigger.kind != "stop" or intent.trigger.direction != expected_direction:
            raise ValueError("pending trigger has invalid stop direction")
    for reference in (intent.trigger, intent.invalidation):
        assert reference is not None  # guaranteed by TradeIntentV2
        frame = windows.get(reference.source.timeframe)
        bars = frame["bars"] if frame else []
        index = reference.source.bar_index
        if index >= len(bars):
            raise ValueError("entry reference cites unavailable bar")
        bar = bars[index]
        if (
            bar["open_time_ms"] != reference.source.open_time_ms
            or bar["close_time_ms"] != reference.source.close_time_ms
            or bar[reference.price_field] != reference.price
        ):
            raise ValueError("entry reference does not match supplied closed bar")


@dataclass
class TraderConsumer:
    agent_key: str
    source: CandleSource
    store: BrooksStore
    events: EventBus
    user_id: int | None = None
    timeout_sec: float = 180
    shadow_mode: bool = True

    async def handle(self, event: BrooksEvent) -> TradeIntentV2:
        if event.type != EventType.H1_BAR_CLOSED:
            raise ValueError("Trader only handles H1_BAR_CLOSED")
        decision_time_ms = _decision_time(event)
        latest = self.store.read_latest("htf")
        htf = MarketContextV1.model_validate(latest) if latest else None
        if htf and (htf.symbol != event.symbol or htf.decision_time_ms > decision_time_ms):
            htf = None
        market_tools = TraderMarketTools(
            source=self.source, decision_time_ms=decision_time_ms, market_context=htf,
        )
        # The copied entry skill's production profile requires each closed
        # window. The market gate rejects forming/future bars and short history.
        windows = {
            label: {
                "interval": interval,
                "bars": await market_tools.get_closed_candles(event.symbol, interval, 120),
            }
            for label, interval in (("H4", "4h"), ("H1", "1h"), ("M15", "15m"))
        }
        if windows["H1"]["bars"][-1]["close_time_ms"] not in {
            decision_time_ms, decision_time_ms - 1,
        }:
            raise ValueError("H1 close event lacks its closed decision bar")
        context: dict[str, Any] = {
            "schema": "brooks.trader-market-input.v1",
            "role": "TRADER",
            "symbol": event.symbol,
            "decision_time_ms": decision_time_ms,
            "timeframes": windows,
        }
        if htf:
            context["higher_timeframe_context"] = htf.model_dump()
        intent = await run_role(
            "TRADER",
            agent_key=self.agent_key,
            prompt=context,
            output_model=TradeIntentV2,
            market_tools=bind_symbol_tools(event.symbol, {
                "get_closed_candles": market_tools.get_closed_candles,
                "get_market_context": market_tools.get_market_context,
                "get_recent_structure": market_tools.get_recent_structure,
                "get_volatility": market_tools.get_volatility,
            }),
            timeout_sec=self.timeout_sec,
            user_id=self.user_id,
        )
        if intent.symbol != event.symbol or intent.decision_time_ms != decision_time_ms:
            raise ValueError("Trader output symbol or decision time mismatch")
        if not {"H4", "H1", "M15"}.issubset(intent.context_timeframes_used):
            raise ValueError("Trader output lacks production timeframe coverage")
        _validate_references(intent, windows)
        self.store.save_trader_intent(intent.model_dump())
        await self.events.publish(BrooksEvent(
            type=EventType.TRADER_INTENT_CREATED,
            symbol=event.symbol,
            correlation_id=event.correlation_id or str(uuid4()),
            causation_id=event.event_id,
            payload={"intent": intent.model_dump(), "shadow_mode": self.shadow_mode},
        ))
        return intent

    async def run(self) -> None:
        queue = self.events.subscribe([EventType.H1_BAR_CLOSED])
        try:
            while True:
                event = await queue.get()
                try:
                    await self.handle(event)
                except Exception:
                    log.exception("Brooks Trader failed for %s event %s", event.symbol, event.event_id)
        finally:
            self.events.unsubscribe(queue)
