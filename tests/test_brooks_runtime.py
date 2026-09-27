"""Brooks runtime lifecycle and durable event-state boundaries."""

from __future__ import annotations

import asyncio
import json

import pytest

from condor.agents.agent import Agent
from condor.agents.config import AgentConfig
from condor.agents.engine import TickEngine
from condor.agents.strategy import Strategy
from condor.brooks.config import BrooksConfig
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.store import BrooksStore
from condor.brooks.supervisor import BrooksSupervisor


def test_brooks_config_keeps_loop_default_and_wake_defaults():
    assert AgentConfig().execution_mode == "loop"
    assert AgentConfig(execution_mode="brooks_agents").execution_mode == "brooks_agents"
    config = BrooksConfig.from_engine_config({})
    assert config.trader.timeframe == "1h"
    assert config.trader.wake_offset_sec == 2
    assert config.htf.timeframe == "1d"
    assert config.htf.wake_offset_sec == 3
    assert config.pm.frequency_sec == 60
    assert config.position_watcher.frequency_sec == 10


def test_brooks_engine_starts_without_tick_or_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_REPORTS_DIR", str(tmp_path / "reports"))
    strategy = Strategy(agent_slug="brooks_test", name="Runtime")
    strategy.home.mkdir(parents=True, exist_ok=True)
    engine = TickEngine(
        agent=Agent(slug="brooks_test", name="Brooks test", agent_key="test"),
        strategy=strategy,
        config={"execution_mode": "brooks_agents"},
        chat_id=1,
        user_id=1,
    )

    async def forbidden_tick():
        raise AssertionError("legacy tick was called")

    monkeypatch.setattr(engine, "_tick", forbidden_tick)

    async def exercise():
        await engine.start()
        assert engine.is_running
        assert isinstance(engine._brooks_supervisor, BrooksSupervisor)
        assert engine._brooks_supervisor.is_running
        assert engine._brooks_supervisor.store.events_path.exists()
        engine.pause()
        assert engine.status == "paused"
        engine.resume()
        assert engine.status == "running"
        await engine.stop()
        assert not engine.is_running
        assert not engine._brooks_supervisor.is_running
        assert engine._task.done()

    asyncio.run(exercise())


def test_event_bus_fans_out_only_after_durable_append(tmp_path, monkeypatch):
    store = BrooksStore(tmp_path)
    bus = EventBus(store)
    all_events = bus.queue
    trader = bus.subscribe({EventType.H1_BAR_CLOSED})
    htf = bus.subscribe({EventType.D1_BAR_CLOSED})
    first = BrooksEvent(
        type=EventType.H1_BAR_CLOSED,
        symbol="BTC-USDT",
        payload={"decision_time_ms": 123},
        correlation_id="run-1",
    )
    second = BrooksEvent(type=EventType.D1_BAR_CLOSED, symbol="BTC-USDT")

    async def exercise():
        await bus.publish(first)
        await bus.publish(second)
        assert await all_events.get() == first
        assert await all_events.get() == second
        assert await trader.get() == first
        assert trader.empty()
        assert await htf.get() == second
        assert htf.empty()
        assert store.read_events() == [first, second]

        def fail(_event):
            raise OSError("disk full")

        monkeypatch.setattr(store, "append_event", fail)
        with pytest.raises(OSError, match="disk full"):
            await bus.publish(first)
        assert trader.empty()
        assert all_events.empty()
        assert store.read_events() == [first, second]

    asyncio.run(exercise())


def test_store_latest_history_and_trade_state_survive_restart(tmp_path):
    store = BrooksStore(tmp_path)
    store.save_trader_intent(
        {"schema": "brooks.trade-intent.v2", "decision": "NO_TRADE"}
    )
    store.save_trader_intent(
        {"schema": "brooks.trade-intent.v2", "decision": "ENTER_LONG"}
    )
    store.save_market_context({"schema": "brooks.market-context.v1", "regime": "trend"})
    store.write_trade_document("trade-1", "binding.json", {"main_position_id": "p1"})
    store.write_trade_document(
        "trade-1", "original_trade_intent.json", {"decision": "ENTER_LONG"}
    )
    store.write_trade_document("trade-1", "hedge_state.json", {"unresolved": False})
    store.write_trade_document(
        "trade-1", "latest_management_intent.json", {"action": "HOLD"}
    )
    store.append_trade_history(
        "trade-1", "management_history.jsonl", {"action": "HOLD"}
    )
    store.append_trade_history("trade-1", "executions.jsonl", {"status": "confirmed"})
    store.flush()

    reopened = BrooksStore(tmp_path)
    assert reopened.read_latest("trader")["decision"] == "ENTER_LONG"
    assert reopened.read_latest("htf")["regime"] == "trend"
    assert len(BrooksStore.read_jsonl(reopened.root / "trader" / "history.jsonl")) == 2
    assert reopened.read_trade_document("trade-1", "binding.json") == {
        "main_position_id": "p1"
    }
    assert (
        len(
            BrooksStore.read_jsonl(
                reopened.root / "trades" / "trade-1" / "executions.jsonl"
            )
        )
        == 1
    )


def test_store_recovers_latest_from_history_and_incomplete_tail(tmp_path):
    store = BrooksStore(tmp_path)
    store.save_trader_intent({"decision": "NO_TRADE"})
    store.save_trader_intent({"decision": "ENTER_SHORT"})
    latest = store.root / "trader" / "latest.json"
    latest.write_text(json.dumps({"decision": "NO_TRADE"}))
    history = store.root / "trader" / "history.jsonl"
    with history.open("ab") as handle:
        handle.write(b'{"partial":')
    recovered = BrooksStore(tmp_path)
    assert recovered.read_latest("trader") == {"decision": "ENTER_SHORT"}
    recovered.save_trader_intent({"decision": "ENTER_LONG"})
    assert [row["decision"] for row in BrooksStore.read_jsonl(history)] == [
        "NO_TRADE",
        "ENTER_SHORT",
        "ENTER_LONG",
    ]


def test_trade_store_rejects_path_traversal(tmp_path):
    store = BrooksStore(tmp_path)
    with pytest.raises(ValueError, match="correlation_id"):
        store.write_trade_document("../outside", "binding.json", {})
    with pytest.raises(ValueError, match="unsupported"):
        store.append_trade_history("trade-1", "other.jsonl", {})
