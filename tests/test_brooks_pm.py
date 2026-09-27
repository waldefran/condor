"""Watcher and PM contract tests with no live exchange or model."""

from __future__ import annotations

import copy
import sys
import types

import pytest

from condor.brooks.pm import PMReadTools, PositionManager
from condor.brooks.position_watcher import PositionWatcher, position_fingerprint


@pytest.fixture
def gate_module(monkeypatch):
    class Bar:
        def __init__(self, close_time_ms):
            self.close_time_ms = close_time_ms

        def model_dump(self, **kwargs):
            return {
                "close_time_ms": self.close_time_ms,
                "high": "2",
                "low": "1",
                "close": "1.5",
            }

    class FakeGate:
        def __init__(self, *, timeframe, decision_time_ms):
            self.timeframe = timeframe
            self.decision_time_ms = decision_time_ms

        def validate(self, raw, *, required_count):
            if len(raw) < required_count or any(
                not bar["closed"] or bar["close_time_ms"] > self.decision_time_ms
                for bar in raw
            ):
                raise ValueError("unclosed or future bar")
            return [Bar(bar["close_time_ms"]) for bar in raw[-required_count:]]

    monkeypatch.setitem(
        sys.modules,
        "condor.brooks.market_tools",
        types.SimpleNamespace(ClosedBarGate=FakeGate),
    )


def snapshot(**changes):
    value = {
        "correlation_id": "trade-1",
        "symbol": "BTC-USDT",
        "main": {"position_id": "main-1", "side": "LONG", "qty": "1.0"},
        "hedge": {"position_id": "hedge-1", "side": "SHORT", "qty": "0"},
        "executors": [{"executor_id": "ex-1", "status": "RUNNING"}],
        "open_orders": [{"order_id": "order-1", "status": "OPEN"}],
        "fills_cursor": "fill-1",
        "recent_fills": [],
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_watcher_emits_only_fingerprint_changes():
    current = snapshot()
    published = []
    watcher = PositionWatcher(lambda: [current], published.append)

    assert [event["type"] for event in await watcher.poll()] == [
        "POSITION_OPENED",
        "ORDER_CHANGED",
    ]
    assert await watcher.poll() == []
    assert len(published) == 2
    assert published[0]["correlation_id"] == "trade-1"
    assert published[0]["schema"] == "condor.brooks.event.v1"
    assert position_fingerprint(
        snapshot(main={"position_id": "main-1", "side": "LONG", "qty": "1.00"})
    ) == position_fingerprint(snapshot())

    current = snapshot(main={"position_id": "main-1", "side": "LONG", "qty": "0.8"})
    assert [event["type"] for event in await watcher.poll()] == ["POSITION_CHANGED"]
    current = snapshot(main={"position_id": "main-1", "side": "SHORT", "qty": "0.8"})
    assert [event["type"] for event in await watcher.poll()] == ["POSITION_CHANGED"]
    current = snapshot(main={"position_id": "main-1", "side": "LONG", "qty": "0"})
    assert [event["type"] for event in await watcher.poll()] == ["POSITION_CLOSED"]


@pytest.mark.asyncio
async def test_watcher_tracks_hedge_order_executor_and_fill_cursor():
    current = snapshot()
    watcher = PositionWatcher(
        lambda: [current], lambda event: None, initial_snapshots=[current]
    )
    assert await watcher.poll() == []

    current = snapshot(hedge={"position_id": "hedge-1", "side": "SHORT", "qty": "0.3"})
    assert [event["type"] for event in await watcher.poll()] == ["HEDGE_OPENED"]
    current = snapshot(hedge={"position_id": "hedge-1", "side": "SHORT", "qty": "0.4"})
    assert [event["type"] for event in await watcher.poll()] == ["HEDGE_CHANGED"]
    current = snapshot(hedge={"position_id": "hedge-1", "side": "SHORT", "qty": "0"})
    assert [event["type"] for event in await watcher.poll()] == ["HEDGE_REMOVED"]
    current = snapshot(executors=[{"executor_id": "ex-1", "status": "CLOSED"}])
    assert [event["type"] for event in await watcher.poll()] == ["ORDER_CHANGED"]
    current = snapshot(fills_cursor="fill-2", recent_fills=[{"fill_id": "fill-2"}])
    events = await watcher.poll()
    assert [event["type"] for event in events] == ["ORDER_CHANGED", "FILL"]
    assert events[-1]["payload"]["fills_cursor"] == "fill-2"


@pytest.mark.asyncio
async def test_watcher_rejects_ambiguous_bindings_and_invalid_quantities():
    watcher = PositionWatcher(lambda: [snapshot(), snapshot()], lambda event: None)
    with pytest.raises(ValueError, match="duplicate Brooks binding"):
        await watcher.poll()
    with pytest.raises(ValueError, match="nonnegative"):
        position_fingerprint(snapshot(main={"side": "LONG", "qty": "-1"}))
    with pytest.raises(ValueError, match="ownership id"):
        position_fingerprint(snapshot(main={"side": "LONG", "qty": "1"}))
    with pytest.raises(ValueError, match="correlation_id"):
        position_fingerprint({"symbol": "BTC-USDT"})


@pytest.mark.asyncio
async def test_pm_read_tools_are_symbol_and_candle_bounded(gate_module):
    calls = []
    audited = []

    async def source(symbol, timeframe, limit):
        calls.append((symbol, timeframe, limit))
        return [{"closed": True, "close_time_ms": 1000} for _ in range(limit)]

    tools = PMReadTools(
        context={
            "correlation_id": "trade-1",
            "symbol": "BTC-USDT",
            "decision_time_ms": 1000,
        },
        candle_source=source,
        record_market_read=lambda correlation_id, record: audited.append(
            (correlation_id, record)
        ),
    )
    assert len(await tools.get_candles("BTC-USDT", "1h", 30)) == 30
    assert calls == [("BTC-USDT", "1h", 31)]
    assert audited[0][1]["accepted"] is True
    for symbol, timeframe, limit in (
        ("ETH-USDT", "1h", 5),
        ("BTC-USDT", "5m", 5),
        ("BTC-USDT", "1h", 31),
        ("BTC-USDT", "1h", 0),
    ):
        with pytest.raises(ValueError):
            await tools.get_candles(symbol, timeframe, limit)
    assert len(calls) == 1

    async def future_source(symbol, timeframe, limit):
        return [{"closed": False, "close_time_ms": 2000} for _ in range(limit)]

    blocked = PMReadTools(
        context={
            "correlation_id": "trade-1",
            "symbol": "BTC-USDT",
            "decision_time_ms": 1000,
        },
        candle_source=future_source,
        record_market_read=lambda correlation_id, record: audited.append(
            (correlation_id, record)
        ),
    )
    with pytest.raises(ValueError, match="unclosed"):
        await blocked.get_candles("BTC-USDT", "1h", 5)
    assert audited[-1][1]["accepted"] is False
    assert {tool.__name__ for tool in tools.as_tools()} == {
        "get_market_context",
        "get_recent_structure",
        "get_volatility",
        "get_latest_trader_intent",
        "get_original_trade_intent",
        "get_position_state",
        "get_executor_state",
        "get_open_orders",
        "get_recent_fills",
        "get_candles",
    }


@pytest.mark.asyncio
async def test_pm_wakes_only_for_bound_positions_and_runs_read_only_tool_loop(
    monkeypatch, gate_module
):
    class FakeDecision:
        def __init__(self, action="HOLD", decision_time_ms=1000):
            self.action = action
            self.decision_time_ms = decision_time_ms
            self.position_ids = []

        @classmethod
        def model_validate(cls, value):
            return cls(value["action"], value["decision_time_ms"])

        def model_dump(self, **kwargs):
            return {"action": self.action, "decision_time_ms": self.decision_time_ms}

    monkeypatch.setitem(
        sys.modules,
        "condor.brooks.contracts",
        types.SimpleNamespace(ManagementDecisionV2=FakeDecision),
    )
    context = {
        "correlation_id": "trade-1",
        "symbol": "BTC-USDT",
        "decision_time_ms": 1000,
        "position_active": True,
        "position": {"qty": "1"},
        "recent_fills": list(range(50)),
        "management_history": list(range(50)),
    }
    saved, published = [], []

    class FakeRunner:
        async def run(self, role, **kwargs):
            assert role == "POSITION_MANAGER"
            assert len(kwargs["prompt"]["recent_fills"]) == 10
            assert len(kwargs["prompt"]["management_history"]) == 10
            assert "candles" not in kwargs["prompt"]
            by_name = kwargs["market_tools"]
            assert await by_name["get_position_state"]() == {"qty": "1"}
            assert len(await by_name["get_candles"]("BTC-USDT", "1h", 5)) == 5
            return {"action": "HOLD", "decision_time_ms": 1000}

    async def candle_source(symbol, timeframe, limit):
        return [{"closed": True, "close_time_ms": 1000} for _ in range(limit)]

    manager = PositionManager(
        runner=FakeRunner(),
        load_context=lambda correlation_id: copy.deepcopy(context),
        save_decision=lambda correlation_id, decision: saved.append(
            (correlation_id, decision)
        ),
        publish=published.append,
        candle_source=candle_source,
        record_market_read=lambda correlation_id, record: None,
        list_active_correlations=lambda symbol: ["trade-1", "trade-1"],
    )
    event = {"type": "POSITION_CHANGED", "correlation_id": "trade-1", "event_id": "e-1"}
    decision = await manager.handle_event(event)
    assert decision.action == "HOLD"
    assert saved == [("trade-1", decision)]
    assert published[0]["type"] == "MANAGEMENT_INTENT_CREATED"
    assert published[0]["causation_id"] == "e-1"
    assert (
        len(await manager.handle_event({"type": "PM_TIMER", "event_id": "timer-1"}))
        == 1
    )
    assert (
        len(
            await manager.handle_event(
                {
                    "type": "TRADER_INTENT_CREATED",
                    "symbol": "BTC-USDT",
                    "event_id": "trader-1",
                }
            )
        )
        == 1
    )
    assert (
        await manager.handle_event(
            {**event, "type": "MARKET_CONTEXT_UPDATED", "symbol": "ETH-USDT"}
        )
        is None
    )
    context["position_active"] = False
    assert (
        await manager.handle_event({**event, "type": "TRADER_INTENT_CREATED"}) is None
    )
    assert (
        await manager.handle_event({**event, "type": "MARKET_CONTEXT_UPDATED"}) is None
    )
    assert await manager.handle_event({**event, "type": "EXECUTION_SUBMITTED"}) is None
    assert len(saved) == 3


@pytest.mark.asyncio
async def test_pm_exposes_no_write_tools(gate_module):
    tools = PMReadTools(
        context={
            "correlation_id": "trade-1",
            "symbol": "BTC-USDT",
            "decision_time_ms": 1000,
        },
        candle_source=lambda symbol, timeframe, limit: [],
        record_market_read=lambda correlation_id, record: None,
    )
    names = {tool.__name__ for tool in tools.as_tools()}
    assert names == set(tools.named_tools())
    assert not names & {
        "create_position_executor",
        "create_order_executor",
        "create_grid_executor",
        "create_dca_executor",
        "stop_executor",
        "manage_bots",
        "manage_controllers",
        "place_order",
        "cancel_order",
        "close_position",
    }


@pytest.mark.asyncio
async def test_pm_unknown_action_fails_closed_to_blocked(monkeypatch, gate_module):
    class FakeDecision:
        def __init__(self, action="HOLD", decision_time_ms=1000):
            self.action = action
            self.decision_time_ms = decision_time_ms
            self.position_ids = []

        @classmethod
        def model_validate(cls, value):
            return cls(value["action"], value["decision_time_ms"])

        def model_dump(self, **kwargs):
            return {"action": self.action, "decision_time_ms": self.decision_time_ms}

    monkeypatch.setitem(
        sys.modules,
        "condor.brooks.contracts",
        types.SimpleNamespace(ManagementDecisionV2=FakeDecision),
    )
    saved, published = [], []

    class UnknownRunner:
        async def run(self, role, **kwargs):
            assert "create_position_executor" not in kwargs["market_tools"]
            return {"action": "BUY", "decision_time_ms": 1000}

    async def candle_source(symbol, timeframe, limit):
        return [{"closed": True, "close_time_ms": 1000} for _ in range(limit)]

    manager = PositionManager(
        runner=UnknownRunner(),
        load_context=lambda correlation_id: {
            "correlation_id": "trade-1",
            "symbol": "BTC-USDT",
            "decision_time_ms": 1000,
            "position_active": True,
        },
        save_decision=lambda correlation_id, decision: saved.append(
            (correlation_id, decision)
        ),
        publish=published.append,
        candle_source=candle_source,
        record_market_read=lambda correlation_id, record: None,
    )
    decision = await manager.handle_event(
        {"type": "POSITION_CHANGED", "correlation_id": "trade-1", "event_id": "e-1"}
    )
    assert decision.action == "MANAGEMENT_BLOCKED"
    assert saved == [("trade-1", decision)]
    assert published[0]["payload"]["action"] == "MANAGEMENT_BLOCKED"

    class GarbageRunner:
        async def run(self, role, **kwargs):
            return {"nonsense": True}

    broken = PositionManager(
        runner=GarbageRunner(),
        load_context=lambda correlation_id: {
            "correlation_id": "trade-1",
            "symbol": "BTC-USDT",
            "decision_time_ms": 1000,
            "position_active": True,
        },
        save_decision=lambda correlation_id, decision: saved.append(
            (correlation_id, decision)
        ),
        publish=published.append,
        candle_source=candle_source,
        record_market_read=lambda correlation_id, record: None,
    )
    with pytest.raises(ValueError, match="invalid management decision"):
        await broken.handle_event(
            {"type": "POSITION_CHANGED", "correlation_id": "trade-1", "event_id": "e-2"}
        )
    assert len(saved) == 1
    assert len(published) == 1


@pytest.mark.asyncio
async def test_pm_initial_context_has_no_candle_dump(monkeypatch, gate_module):
    class FakeDecision:
        def __init__(self, action="HOLD", decision_time_ms=1000):
            self.action = action
            self.decision_time_ms = decision_time_ms
            self.position_ids = []

        @classmethod
        def model_validate(cls, value):
            return cls(value["action"], value["decision_time_ms"])

        def model_dump(self, **kwargs):
            return {"action": self.action, "decision_time_ms": self.decision_time_ms}

    monkeypatch.setitem(
        sys.modules,
        "condor.brooks.contracts",
        types.SimpleNamespace(ManagementDecisionV2=FakeDecision),
    )
    seen = {}

    class CaptureRunner:
        async def run(self, role, **kwargs):
            seen.update(kwargs["prompt"])
            return {"action": "HOLD", "decision_time_ms": 1000}

    async def candle_source(symbol, timeframe, limit):
        return [{"closed": True, "close_time_ms": 1000} for _ in range(limit)]

    manager = PositionManager(
        runner=CaptureRunner(),
        load_context=lambda correlation_id: {
            "correlation_id": "trade-1",
            "symbol": "BTC-USDT",
            "decision_time_ms": 1000,
            "position_active": True,
            "position": {"qty": "1"},
            "candles": [{"close": "1"} for _ in range(200)],
            "ohlc": [{"close": "1"} for _ in range(200)],
            "bars": [{"close": "1"} for _ in range(200)],
            "open_orders": [{"order_id": str(i)} for i in range(50)],
            "recent_fills": list(range(50)),
        },
        save_decision=lambda correlation_id, decision: None,
        publish=lambda event: None,
        candle_source=candle_source,
        record_market_read=lambda correlation_id, record: None,
    )
    await manager.handle_event(
        {"type": "POSITION_CHANGED", "correlation_id": "trade-1", "event_id": "e-1"}
    )
    assert "candles" not in seen
    assert "ohlc" not in seen
    assert "bars" not in seen
    assert len(seen["open_orders"]) == 20
    assert len(seen["recent_fills"]) == 10
