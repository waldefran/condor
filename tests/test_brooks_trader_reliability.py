"""Focused tests for the Brooks Trader backend and decision-cycle boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from pydantic import BaseModel

from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.llm_coordination import (
    backend_resource_key,
    run_coordinated_role,
)
from condor.brooks.store import BrooksStore


def _bars(symbol: str, timeframe: str, limit: int, decision_time_ms: int):
    interval = {
        "15m": 900_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }[timeframe]
    return [
        {
            "open_time_ms": decision_time_ms - (limit - index) * interval,
            "close_time_ms": decision_time_ms - (limit - index - 1) * interval - 1,
            "open": "100",
            "high": "105",
            "low": "95",
            "close": "101",
            "closed": True,
        }
        for index in range(limit)
    ]


class _Source:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int):
        self.calls.append((symbol, timeframe, limit))
        interval_ms = {
            "15m": 900_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
            "1d": 86_400_000,
        }[timeframe]
        # Market tools ask for one extra bar, so the decision is inferred from
        # the latest closed boundary and any forming/future bar is excluded.
        latest_open = (self.decision_time_ms // interval_ms) * interval_ms
        return [
            {
                "open_time_ms": latest_open - (limit - index) * interval_ms,
                "close_time_ms": latest_open - (limit - index - 1) * interval_ms - 1,
                "open": "100",
                "high": "105",
                "low": "95",
                "close": "101",
                "closed": True,
            }
            for index in range(limit)
        ]

    decision_time_ms: int = 0


def _event(symbol: str, decision_time_ms: int) -> BrooksEvent:
    return BrooksEvent(
        type=EventType.H1_BAR_CLOSED,
        symbol=symbol,
        payload={"decision_time_ms": decision_time_ms},
    )


def _no_trade(symbol: str, decision_time_ms: int):
    from condor.brooks.contracts import TradeIntentV2

    return TradeIntentV2.model_validate(
        {
            "schema": "brooks.trade-intent.v2",
            "role": "TRADER",
            "decision": "NO_TRADE",
            "symbol": symbol,
            "decision_time_ms": decision_time_ms,
            "market_context": {"regime": "unclear"},
            "setup": {"no_trade_reason": "no_trigger"},
            "decision_timeframe": None,
            "context_timeframes_used": ["H1", "M15"],
            "entry_mechanism": "none",
            "trigger": None,
            "invalidation": None,
            "evidence_for": ["No actionable trigger is present."],
            "evidence_against": ["A later bar could create a setup."],
            "qualitative_confidence": "medium",
            "uncertainty": ["The next closed bar is unknown."],
            "conditions_that_change_market_read": ["A new signal bar forms."],
        }
    )


def _entry(symbol: str, decision_time_ms: int, m15: list[dict]):
    from condor.brooks.contracts import TradeIntentV2

    bar = m15[119]
    source = {
        "timeframe": "M15",
        "bar_index": 119,
        "open_time_ms": bar["open_time_ms"],
        "close_time_ms": bar["close_time_ms"],
    }
    return TradeIntentV2.model_validate(
        {
            "schema": "brooks.trade-intent.v2",
            "role": "TRADER",
            "decision": "ENTER_LONG",
            "symbol": symbol,
            "decision_time_ms": decision_time_ms,
            "market_context": {"regime": "transition"},
            "setup": {
                "type": "breakout",
                "trigger_status": "pending",
                "signal_quality": "clear",
                "location_assessment": "favorable",
                "no_trade_reason": None,
            },
            "decision_timeframe": "M15",
            "context_timeframes_used": ["H4", "H1", "M15"],
            "entry_mechanism": "breakout",
            "trigger": {
                "kind": "stop",
                "direction": "above",
                "reference": "signal_bar_high",
                "price_field": "high",
                "price": bar["high"],
                "source": source,
            },
            "invalidation": {
                "reference": "signal_bar_low",
                "price_field": "low",
                "price": bar["low"],
                "source": source,
            },
            "evidence_for": ["The signal bar closes near its high."],
            "evidence_against": ["Follow-through remains unknown."],
            "qualitative_confidence": "medium",
            "uncertainty": ["A breakout can fail."],
            "conditions_that_change_market_read": ["The stop trigger is rejected."],
        }
    )


class _Output(BaseModel):
    ok: bool


@pytest.mark.asyncio
async def test_same_backend_calls_serialize_and_trader_gets_priority():
    resource = backend_resource_key("ollama:qwen3")
    assert resource == backend_resource_key("ollama:llama3")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    maximum = 0
    observed: list[str] = []

    async def runner(role, prompt, output_model, market_tools, *, label, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        observed.append(label)
        try:
            if label == "D1":
                first_started.set()
                await release_first.wait()
            await asyncio.sleep(0)
            return output_model(ok=True)
        finally:
            active -= 1

    async def call(label: str, priority: int):
        return await run_coordinated_role(
            "TRADER" if label == "H1" else "CONTEXT_ANALYST",
            {"symbol": "BTC-USDT"},
            _Output,
            {},
            agent_key="ollama:qwen3",
            backend_key=resource,
            priority=priority,
            runner=runner,
            label=label,
            max_role_attempts=1,
        )

    d1 = asyncio.create_task(call("D1", 10))
    await first_started.wait()
    h4 = asyncio.create_task(call("H4", 10))
    h1 = asyncio.create_task(call("H1", 0))
    await asyncio.sleep(0.01)  # let both calls enter the backend queue
    release_first.set()
    await asyncio.gather(d1, h4, h1)
    assert maximum == 1
    assert observed == ["D1", "H1", "H4"]


@pytest.mark.asyncio
async def test_context_retry_yields_backend_to_queued_trader():
    from condor.brooks.llm_coordination import run_coordinated_role

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    observed = []

    async def runner(role, prompt, output_model, market_tools, **kwargs):
        observed.append(role)
        if role == "CONTEXT_ANALYST" and observed.count(role) == 1:
            first_started.set()
            await release_first.wait()
            raise TimeoutError("context backend timed out")
        return output_model(ok=True)

    async def context():
        return await run_coordinated_role(
            "CONTEXT_ANALYST", {"symbol": "BTC-USDT"}, _Output, {},
            agent_key="ollama:qwen3", priority=10, runner=runner,
            max_role_attempts=2, retry_backoff_sec=0,
        )

    async def trader():
        return await run_coordinated_role(
            "TRADER", {"symbol": "BTC-USDT"}, _Output, {},
            agent_key="ollama:qwen3", priority=0, runner=runner,
            max_role_attempts=1,
        )

    context_task = asyncio.create_task(context())
    await first_started.wait()
    trader_task = asyncio.create_task(trader())
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(context_task, trader_task)
    assert observed == ["CONTEXT_ANALYST", "TRADER", "CONTEXT_ANALYST"]


@pytest.mark.asyncio
async def test_trader_timeout_retry_reuses_frozen_packet_and_persists_once(
    tmp_path, monkeypatch
):
    from condor.brooks import trader

    decision_time_ms = 120 * 4 * 3_600_000
    source = _Source()
    source.decision_time_ms = decision_time_ms
    store = BrooksStore(tmp_path)
    bus = EventBus(store)
    packet_hashes: list[str] = []
    prompts_seen: list[dict] = []
    calls = 0
    intent = _no_trade("BTC-USDT", decision_time_ms)

    async def fake_run(role, prompt, output_model, market_tools, **kwargs):
        nonlocal calls
        calls += 1
        snapshot = json.dumps(prompt, sort_keys=True, separators=(",", ":"))
        packet_hashes.append(hashlib.sha256(snapshot.encode()).hexdigest())
        prompts_seen.append(prompt)
        assert set(prompt["raw"]) == {"H1", "M15"}
        assert len(prompt["raw"]["H1"]) == len(prompt["raw"]["M15"]) == 120
        assert all(
            bar["close_time_ms"] <= decision_time_ms
            for bars in prompt["raw"].values()
            for bar in bars
        )
        if calls == 1:
            # A client that mutates its argument cannot mutate the persisted
            # canonical snapshot used to build attempt two.
            prompt["raw"]["H1"][0]["close"] = "999"
            raise TimeoutError("provider timed out")
        return intent

    monkeypatch.setattr(trader, "run_role", fake_run)
    consumer = trader.TraderConsumer(
        "ollama:qwen3",
        source,
        store,
        bus,
        timeout_sec=1,
        max_role_attempts=2,
        retry_backoff_sec=0,
    )
    event = _event("BTC-USDT", decision_time_ms)
    restarted = trader.TraderConsumer(
        "ollama:qwen3",
        source,
        store,
        bus,
        timeout_sec=1,
        max_role_attempts=2,
        retry_backoff_sec=0,
    )
    assert await restarted.handle(event) == intent
    assert calls == 2
    assert packet_hashes[0] == packet_hashes[1]
    assert prompts_seen[1]["raw"]["H1"][0]["close"] == "101"
    assert len(source.calls) == 2  # H1 and M15 were frozen only once.

    cycle_id = trader.DecisionCycleStore.identity("BTC-USDT", decision_time_ms)
    cycle = consumer._cycle_store.get(cycle_id)
    assert cycle["status"] == "completed" and cycle["attempt"] == 2
    assert cycle["packet_hash"] == packet_hashes[0]
    assert len(store.read_jsonl(store.root / "trader" / "history.jsonl")) == 1
    assert [event.type for event in store.read_events()].count(EventType.TRADER_INTENT_CREATED) == 1
    assert await consumer.handle(event) == intent
    assert calls == 2
    assert len(store.read_jsonl(store.root / "trader" / "history.jsonl")) == 1


@pytest.mark.asyncio
async def test_final_timeout_persists_failure_and_later_h1_cycle_runs(
    tmp_path, monkeypatch
):
    from condor.brooks import trader

    decision_time_ms = 120 * 4 * 3_600_000
    source = _Source()
    source.decision_time_ms = decision_time_ms
    store = BrooksStore(tmp_path)
    bus = EventBus(store)
    calls: list[int] = []

    async def fake_run(role, prompt, output_model, market_tools, **kwargs):
        calls.append(prompt["decision_time_ms"])
        if len(calls) <= 2:
            raise TimeoutError("provider timed out")
        return _no_trade("BTC-USDT", prompt["decision_time_ms"])

    monkeypatch.setattr(trader, "run_role", fake_run)
    consumer = trader.TraderConsumer(
        "ollama:qwen3",
        source,
        store,
        bus,
        timeout_sec=1,
        max_role_attempts=2,
        retry_backoff_sec=0,
    )
    assert await consumer.handle(_event("BTC-USDT", decision_time_ms)) is None
    first_id = trader.DecisionCycleStore.identity("BTC-USDT", decision_time_ms)
    failed = consumer._cycle_store.get(first_id)
    assert failed["status"] == "failed" and failed["attempt"] == 2
    assert failed["failure"]["error_type"] == "TimeoutError"
    assert store.read_latest("trader") is None
    events = store.read_events()
    assert [row.type for row in events].count(EventType.TRADER_DECISION_FAILED) == 1
    assert not any(row.type == EventType.TRADER_INTENT_CREATED for row in events)

    source.decision_time_ms = decision_time_ms + 3_600_000
    result = await consumer.handle(_event("BTC-USDT", source.decision_time_ms))
    assert result is not None
    second_id = trader.DecisionCycleStore.identity("BTC-USDT", source.decision_time_ms)
    assert consumer._cycle_store.get(second_id)["status"] == "completed"
    assert len(store.read_jsonl(store.root / "trader" / "history.jsonl")) == 1


@pytest.mark.asyncio
async def test_enter_with_missing_h4_context_requires_current_run_raw_h4(
    tmp_path, monkeypatch
):
    from condor.brooks import trader

    decision_time_ms = 120 * 86_400_000
    source = _Source()
    source.decision_time_ms = decision_time_ms
    store = BrooksStore(tmp_path)
    bus = EventBus(store)
    seen_h4: list[int] = []

    async def fake_run(role, prompt, output_model, market_tools, **kwargs):
        h4 = await market_tools["get_closed_candles"](
            symbol="BTC-USDT", timeframe="4h", limit=120
        )
        seen_h4.append(len(h4))
        audit = kwargs["tool_audit"]
        audit.append(
            {
                "role": "TRADER",
                "tool": "get_closed_candles",
                "arguments": {"symbol": "BTC-USDT", "timeframe": "4h", "limit": 120},
                "status": "started",
            }
        )
        audit[-1]["status"] = "completed"
        audit[-1]["result_type"] = "list"
        audit[-1]["result_count"] = len(h4)
        return _entry("BTC-USDT", decision_time_ms, prompt["raw"]["M15"])

    monkeypatch.setattr(trader, "run_role", fake_run)
    consumer = trader.TraderConsumer(
        "ollama:qwen3",
        source,
        store,
        bus,
        timeout_sec=1,
        retry_backoff_sec=0,
    )
    result = await consumer.handle(_event("BTC-USDT", decision_time_ms))
    assert result is not None and result.decision == "ENTER_LONG"
    assert seen_h4 == [120]
    cycle_id = trader.DecisionCycleStore.identity("BTC-USDT", decision_time_ms)
    audit = consumer._cycle_store.get(cycle_id)["tool_audit"]
    assert any(
        row.get("tool") == "get_closed_candles"
        and row.get("arguments", {}).get("timeframe") == "4h"
        and row.get("status") == "completed"
        for row in audit
    )


@pytest.mark.asyncio
async def test_enter_without_h4_raw_read_fails_closed(tmp_path, monkeypatch):
    from condor.brooks import trader

    decision_time_ms = 120 * 86_400_000
    source = _Source()
    source.decision_time_ms = decision_time_ms
    store = BrooksStore(tmp_path)
    bus = EventBus(store)

    async def fake_run(role, prompt, output_model, market_tools, **kwargs):
        return _entry("BTC-USDT", decision_time_ms, prompt["raw"]["M15"])

    monkeypatch.setattr(trader, "run_role", fake_run)
    consumer = trader.TraderConsumer(
        "ollama:qwen3", source, store, bus, timeout_sec=1, retry_backoff_sec=0
    )
    assert await consumer.handle(_event("BTC-USDT", decision_time_ms)) is None
    cycle_id = trader.DecisionCycleStore.identity("BTC-USDT", decision_time_ms)
    assert consumer._cycle_store.get(cycle_id)["status"] == "failed"
    assert store.read_latest("trader") is None
    events = store.read_events()
    assert [row.type for row in events].count(EventType.TRADER_DECISION_FAILED) == 1
    assert not any(row.type == EventType.TRADER_INTENT_CREATED for row in events)
