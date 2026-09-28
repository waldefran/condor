"""Brooks role isolation and independent market consumers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from condor.brooks import agent_runner


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []
        self.started = False
        self.stopped = False
        self.working_dir = None

    async def start(self):
        self.started = True
        assert not (Path(self.working_dir) / ".mcp.json").exists()

    async def prompt(self, prompt):
        self.prompts.append(prompt)
        return next(self.responses)

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_role_runner_uses_read_tools_and_validates_output(monkeypatch):
    client = _Client(
        [
            json.dumps({"tool": "get_closed_candles", "arguments": {"limit": 2}}),
            json.dumps({"decision": "NO_TRADE"}),
        ]
    )
    config = {}

    def make_client(*args, **kwargs):
        config.update(kwargs)
        return client

    monkeypatch.setattr(agent_runner, "build_llm_client", make_client)
    result = await agent_runner.run_role(
        "TRADER",
        agent_key="claude-acp:sonnet",
        prompt={"symbol": "BTC-USDT"},
        output_model=_Output,
        market_tools={"get_closed_candles": lambda limit: [{"close": "100"}] * limit},
    )
    assert result.decision == "NO_TRADE"
    assert client.started and client.stopped
    assert "Read tool get_closed_candles result" in client.prompts[1]
    assert config["mcp_servers"] == []
    assert config["allowed_tools"] == []
    assert client.working_dir.startswith("/tmp/condor-brooks-")
    assert (await config["permission_callback"]({}, []))["outcome"][
        "outcome"
    ] == "cancelled"
    assert "brooks-trade-entry" in config["system_prompt"]


@pytest.mark.asyncio
async def test_role_runner_rejects_private_context_and_write_tools():
    with pytest.raises(ValueError, match="private"):
        await agent_runner.run_role(
            "TRADER",
            agent_key="claude-code",
            prompt={"market": {"account": {"equity": 1}}},
            output_model=_Output,
            market_tools={},
        )
    with pytest.raises(ValueError, match="forbidden"):
        await agent_runner.run_role(
            "TRADER",
            agent_key="claude-code",
            prompt={},
            output_model=_Output,
            market_tools={"create_order_executor": lambda: None},
        )


@pytest.mark.asyncio
async def test_role_runner_rejects_unknown_tool_request(monkeypatch):
    client = _Client([json.dumps({"tool": "get_position_state", "arguments": {}})])
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)
    with pytest.raises(agent_runner.RoleRunError, match="unavailable"):
        await agent_runner.run_role(
            "HTF_ANALYST",
            agent_key="claude-code",
            prompt={"symbol": "BTC-USDT"},
            output_model=_Output,
            market_tools={},
        )
    assert client.stopped


@pytest.mark.asyncio
async def test_symbol_bound_market_tool_rejects_other_symbol():
    tools = agent_runner.bind_symbol_tools(
        "BTC-USDT",
        {"get_closed_candles": lambda symbol: symbol},
    )
    assert await tools["get_closed_candles"](symbol="BTC-USDT") == "BTC-USDT"
    with pytest.raises(agent_runner.RoleRunError, match="symbol mismatch"):
        await tools["get_closed_candles"](symbol="ETH-USDT")


@pytest.mark.asyncio
async def test_role_runner_timeout_stops_client(monkeypatch):
    class SlowClient(_Client):
        async def prompt(self, prompt):
            await asyncio.sleep(1)

    client = SlowClient([])
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)
    with pytest.raises(TimeoutError):
        await agent_runner.run_role(
            "HTF_ANALYST",
            agent_key="claude-code",
            prompt={},
            output_model=_Output,
            market_tools={},
            timeout_sec=0.01,
        )
    assert client.stopped


def _bars(symbol, timeframe, limit, decision_time_ms):
    interval = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[
        timeframe
    ]
    return [
        {
            "open_time_ms": decision_time_ms - (120 - index) * interval,
            "close_time_ms": decision_time_ms - (119 - index) * interval - 1,
            "open": "100",
            "high": "105",
            "low": "95",
            "close": "101",
            "closed": True,
        }
        for index in range(120)
    ]


class _Source:
    def __init__(self, decision_time_ms):
        self.decision_time_ms = decision_time_ms

    async def fetch_candles(self, symbol, timeframe, limit):
        return _bars(symbol, timeframe, limit, self.decision_time_ms)


class _Store:
    def __init__(self):
        self.saved = []
        self.open_position = {"quantity": "1"}

    def read_latest(self, role):
        return None

    def save_trader_intent(self, value):
        self.saved.append(("trader", value))

    def save_market_context(self, value):
        self.saved.append(("htf", value))


class _Bus:
    def __init__(self, store):
        self.store = store
        self.published = []

    async def publish(self, event):
        assert self.store.saved
        self.published.append(event)


@pytest.mark.asyncio
async def test_trader_runs_with_open_position_and_publishes_after_save(monkeypatch):
    from condor.brooks import trader
    from condor.brooks.contracts import TradeIntentV2
    from condor.brooks.events import BrooksEvent, EventType

    decision_time_ms = 120 * 4 * 3_600_000
    store = _Store()
    bus = _Bus(store)
    seen = {}
    intent = TradeIntentV2.model_validate(
        {
            "schema": "brooks.trade-intent.v2",
            "role": "TRADER",
            "decision": "NO_TRADE",
            "symbol": "BTC-USDT",
            "decision_time_ms": decision_time_ms,
            "market_context": {},
            "setup": {"no_trade_reason": "no_trigger"},
            "decision_timeframe": None,
            "context_timeframes_used": ["H4", "H1", "M15"],
            "entry_mechanism": "none",
            "trigger": None,
            "invalidation": None,
            "evidence_for": ["No actionable trigger"],
            "evidence_against": ["Trend could resume"],
            "qualitative_confidence": "medium",
            "uncertainty": ["Next bar unknown"],
            "conditions_that_change_market_read": ["New breakout"],
        }
    )

    async def fake_run(role, prompt, output_model, market_tools, **kwargs):
        seen.update(prompt)
        assert role == "TRADER" and output_model is TradeIntentV2
        assert set(market_tools) == {
            "get_closed_candles",
            "get_market_context",
            "get_recent_structure",
            "get_volatility",
        }
        return intent

    monkeypatch.setattr(trader, "run_role", fake_run)
    consumer = trader.TraderConsumer(
        "claude-code", _Source(decision_time_ms), store, bus
    )
    event = BrooksEvent(
        EventType.H1_BAR_CLOSED,
        "BTC-USDT",
        {"decision_time_ms": decision_time_ms},
    )
    assert await consumer.handle(event) == intent
    assert store.open_position
    assert seen["symbol"] == "BTC-USDT" and "account" not in seen
    assert len(seen["timeframes"]["H1"]["bars"]) == 120
    assert store.saved[0][0] == "trader"
    assert bus.published[0].type == EventType.TRADER_INTENT_CREATED
    assert bus.published[0].payload["shadow_mode"] is True


def test_trader_rejects_invented_entry_reference():
    from condor.brooks import trader
    from condor.brooks.contracts import TradeIntentV2

    decision_time_ms = 120 * 4 * 3_600_000
    bars = _bars("BTC-USDT", "15m", 120, decision_time_ms)
    windows = {"M15": {"bars": bars}}
    source = {
        "timeframe": "M15",
        "bar_index": 119,
        "open_time_ms": bars[-1]["open_time_ms"],
        "close_time_ms": bars[-1]["close_time_ms"],
    }
    base = {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "ENTER_LONG",
        "symbol": "BTC-USDT",
        "decision_time_ms": decision_time_ms,
        "market_context": {},
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
            "price": "105",
            "source": source,
        },
        "invalidation": {
            "reference": "signal_bar_low",
            "price_field": "low",
            "price": "95",
            "source": source,
        },
        "evidence_for": ["Breakout setup"],
        "evidence_against": ["Range overhead"],
        "qualitative_confidence": "medium",
        "uncertainty": ["Follow-through unknown"],
        "conditions_that_change_market_read": ["Breakout failure"],
    }
    trader._validate_references(TradeIntentV2.model_validate(base), windows)
    base["trigger"]["price"] = "104"
    with pytest.raises(ValueError, match="does not match"):
        trader._validate_references(TradeIntentV2.model_validate(base), windows)


@pytest.mark.asyncio
async def test_htf_analyst_independent_d1_persistence(monkeypatch):
    from condor.brooks import htf_analyst
    from condor.brooks.contracts import MarketContextV1
    from condor.brooks.events import BrooksEvent, EventType

    decision_time_ms = 120 * 86_400_000
    store = _Store()
    bus = _Bus(store)
    context = MarketContextV1.model_validate(
        {
            "schema": "brooks.market-context.v1",
            "role": "HTF_ANALYST",
            "symbol": "BTC-USDT",
            "decision_time_ms": decision_time_ms,
            "timeframe": "D1",
            "observations": ["range"],
            "evidence_against": ["bull closes"],
            "uncertainty": ["breakout unknown"],
        }
    )

    async def fake_run(role, prompt, output_model, market_tools, **kwargs):
        assert role == "HTF_ANALYST" and output_model is MarketContextV1
        assert prompt["timeframe"] == "1d" and len(prompt["bars"]) == 120
        assert "get_market_context" not in market_tools
        return context

    monkeypatch.setattr(htf_analyst, "run_role", fake_run)
    consumer = htf_analyst.HTFAnalystConsumer(
        "claude-code",
        _Source(decision_time_ms),
        store,
        bus,
    )
    event = BrooksEvent(
        EventType.D1_BAR_CLOSED,
        "BTC-USDT",
        {"decision_time_ms": decision_time_ms},
    )
    assert await consumer.handle(event) == context
    assert store.saved[0][0] == "htf"
    assert bus.published[0].type == EventType.MARKET_CONTEXT_UPDATED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaking_key",
    [
        "position_id",
        "positionId",
        "position_ids",
        "positionIds",
        "unrealized_pnl",
        "unrealizedPnl",
        "realized_pnl",
        "realizedPnl",
        "available_margin",
        "availableMargin",
        "available_balance",
        "availableBalance",
        "entry_price",
        "entryPrice",
        "entry_prices",
        "entryPrices",
        "open_orders",
        "openOrders",
        "recent_fills",
        "recentFills",
        "trade_history",
        "tradeHistory",
        "management_history",
        "managementHistory",
    ],
)
async def test_role_runner_rejects_compound_and_camel_case_private_keys(leaking_key):
    # Shallow prompt key
    with pytest.raises(ValueError, match="private"):
        await agent_runner.run_role(
            "TRADER",
            agent_key="claude-code",
            prompt={leaking_key: "leaked_value"},
            output_model=_Output,
            market_tools={},
        )

    # Deeply nested prompt key inside dicts and lists
    with pytest.raises(ValueError, match="private"):
        await agent_runner.run_role(
            "HTF_ANALYST",
            agent_key="claude-code",
            prompt={
                "market": {
                    "level1": [
                        {"clean": 1},
                        {"nested": {leaking_key: 123}},
                    ]
                }
            },
            output_model=_Output,
            market_tools={},
        )


@pytest.mark.asyncio
async def test_role_runner_permits_legitimate_public_market_fields(monkeypatch):
    client = _Client([json.dumps({"decision": "NO_TRADE"})])
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)
    prompt = {
        "schema": "brooks.trader-market-input.v1",
        "role": "TRADER",
        "symbol": "BTC-USDT",
        "decision_time_ms": 1700000000000,
        "timeframes": {
            "H1": {
                "bars": [
                    {
                        "open": "100",
                        "high": "105",
                        "low": "95",
                        "close": "102",
                        "volume": "1000",
                        "close_time_ms": 1700000000000,
                        "trades": 50,
                    }
                ]
            }
        },
        "fields": ["ordered_ohlc", "bar_by_bar", "decision_time"],
        "higher_timeframe_context": {
            "market_regime": "broad_channel",
            "always_in_direction": "LONG",
            "observations": ["bar breakout"],
            "uncertainty": "low",
        },
    }
    result = await agent_runner.run_role(
        "TRADER",
        agent_key="claude-code",
        prompt=prompt,
        output_model=_Output,
        market_tools={},
    )
    assert result.decision == "NO_TRADE"


@pytest.mark.asyncio
async def test_role_runner_handles_tool_exception_without_crash(monkeypatch):
    client = _Client(
        [
            json.dumps({"tool": "get_closed_candles", "arguments": {"limit": 5}}),
            json.dumps({"decision": "NO_TRADE"}),
        ]
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)

    def exploding_tool(limit: int):
        raise RuntimeError("simulated candle database timeout")

    result = await agent_runner.run_role(
        "TRADER",
        agent_key="claude-code",
        prompt={"symbol": "BTC-USDT"},
        output_model=_Output,
        market_tools={"get_closed_candles": exploding_tool},
    )
    assert result.decision == "NO_TRADE"
    assert len(client.prompts) == 2
    assert "Tool execution failed: RuntimeError: simulated candle database timeout" in client.prompts[1]


@pytest.mark.asyncio
async def test_role_runner_propagates_role_run_error_from_tool(monkeypatch):
    client = _Client(
        [
            json.dumps({"tool": "get_closed_candles", "arguments": {"symbol": "ETH-USDT"}}),
        ]
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)

    tools = agent_runner.bind_symbol_tools(
        "BTC-USDT",
        {"get_closed_candles": lambda symbol: symbol},
    )

    with pytest.raises(agent_runner.RoleRunError, match="symbol mismatch"):
        await agent_runner.run_role(
            "TRADER",
            agent_key="claude-code",
            prompt={"symbol": "BTC-USDT"},
            output_model=_Output,
            market_tools=tools,
        )

