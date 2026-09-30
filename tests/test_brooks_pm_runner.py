"""PM role-runner boundary (real run_role, mocked LLM client) and PM context."""

from __future__ import annotations

import asyncio
import json

import pytest

from condor.brooks import agent_runner
from condor.brooks.contracts import ManagementDecisionV2

DECISION_MS = 1_000
SYMBOL = "BTC-USDT"


def hold_decision(position_id="main-1"):
    return {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": DECISION_MS,
        "action": "HOLD",
        "position_ids": [position_id],
        "reason": "Position and protection are coherent.",
        "evidence": {
            "observations": ["Venue MAIN leg matches the persisted binding."],
            "evidence_for": ["No unmet protection or margin condition."],
            "evidence_against": ["A fresh venue move could change the read."],
        },
        "risk": {
            "exposure_before": ["BTC-USDT LONG 0.1"],
            "exposure_after": ["BTC-USDT LONG 0.1"],
            "protection_status": "adequate",
            "costs_considered": ["Fees and funding remain applicable."],
            "uncertainty": "low",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": None,
        "market_analysis_request": None,
        "conditions_that_change_action": [
            "A fill, cancellation, or quantity mismatch requires reconciliation."
        ],
    }


def pm_context(**changes):
    value = {
        "correlation_id": "trade-1",
        "symbol": SYMBOL,
        "decision_time_ms": DECISION_MS,
        "position_active": True,
        "positions": [
            {
                "position_id": "main-1",
                "symbol": SYMBOL,
                "side": "LONG",
                "quantity": "0.1",
            }
        ],
        "open_orders": [],
        "recent_fills": [],
    }
    value.update(changes)
    return value


class FakeLLMClient:
    """Only the LLM client seam is mocked; run_role itself is real."""

    def __init__(self, script):
        self._script = list(script)
        self.prompts = []
        self.client_kwargs: dict | None = None
        self.started = False
        self.stopped = False
        self.working_dir = None

    async def start(self):
        self.started = True

    async def prompt(self, turn):
        self.prompts.append(turn)
        return self._script.pop(0)

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_pm_default_runner_full_loop_through_real_run_role(monkeypatch):
    """PM context -> real run_role -> tool call -> tool result -> decision."""
    from condor.brooks.pm import PositionManager

    position_marker = {"position_id": "main-1", "qty": "0.1", "side": "LONG"}
    saved, published, audits = [], [], []
    fake = FakeLLMClient(
        [
            json.dumps({"tool": "get_position_state", "arguments": {}}),
            json.dumps(hold_decision()),
        ]
    )

    def make_client(*args, **kwargs):
        fake.client_kwargs = kwargs
        return fake

    monkeypatch.setattr(agent_runner, "build_llm_client", make_client)

    async def candle_source(symbol, timeframe, limit):
        raise AssertionError("no candle read expected on this path")

    manager = PositionManager(
        runner=None,
        load_context=lambda correlation_id: pm_context(
            positions=[position_marker | {"symbol": SYMBOL}]
        ),
        save_decision=lambda correlation_id, decision: saved.append(
            (correlation_id, decision)
        ),
        publish=published.append,
        candle_source=candle_source,
        record_market_read=lambda correlation_id, record: audits.append(record),
        agent_key="test-key",
        timeout_sec=30,
    )
    decision = await manager.handle_event(
        {"type": "POSITION_CHANGED", "correlation_id": "trade-1", "event_id": "e-1"}
    )
    assert isinstance(decision, ManagementDecisionV2)
    assert decision.action == "HOLD"
    assert decision.position_ids == ["main-1"]
    # The tool really executed: its result is visible in the second model turn.
    assert len(fake.prompts) == 2
    assert "Read tool get_position_state result" in fake.prompts[1]
    assert "main-1" in fake.prompts[1]
    assert fake.started and fake.stopped
    assert "brooks-position-management" in fake.client_kwargs["system_prompt"]
    assert saved == [("trade-1", decision)]
    assert published[0]["type"] == "MANAGEMENT_INTENT_CREATED"
    assert published[0]["payload"]["action"] == "HOLD"


@pytest.mark.asyncio
async def test_pm_default_runner_uses_canonical_convention(monkeypatch):
    """The default path must not use the legacy tools=/context= spellings."""
    from condor.brooks.pm import PositionManager

    seen = {}
    real_run_role = agent_runner.run_role

    async def spy(role, prompt, output_model, market_tools, **kwargs):
        seen.update(
            {
                "role": role,
                "prompt": prompt,
                "output_model": output_model,
                "market_tools": market_tools,
                "kwargs": kwargs,
            }
        )
        return await real_run_role(
            role,
            prompt,
            output_model,
            market_tools,
            agent_key=kwargs["agent_key"],
            timeout_sec=kwargs["timeout_sec"],
        )

    monkeypatch.setattr(agent_runner, "run_role", spy)

    fake = FakeLLMClient(
        [
            json.dumps({"tool": "get_position_state", "arguments": {}}),
            json.dumps(hold_decision()),
        ]
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: fake)

    async def candle_source(symbol, timeframe, limit):
        return []

    manager = PositionManager(
        runner=None,
        load_context=lambda correlation_id: pm_context(),
        save_decision=lambda correlation_id, decision: None,
        publish=lambda event: None,
        candle_source=candle_source,
        record_market_read=lambda correlation_id, record: None,
        agent_key="test-key",
    )
    await manager.handle_event(
        {"type": "POSITION_CHANGED", "correlation_id": "trade-1", "event_id": "e-1"}
    )
    assert seen["role"] == "POSITION_MANAGER"
    assert seen["output_model"] is ManagementDecisionV2
    assert set(seen["market_tools"]) >= {"get_position_state", "get_candles"}
    assert seen["prompt"]["correlation_id"] == "trade-1"
    assert seen["kwargs"]["agent_key"] == "test-key"
    assert "tools" not in seen["kwargs"] and "context" not in seen["kwargs"]


# -- production pm_load_context --


class FakeMarketData:
    def __init__(self, outer):
        self._outer = outer

    async def get_prices(self, connector_name, trading_pairs):
        return {"prices": dict(self._outer.prices)}


class FakeTrading:
    def __init__(self, outer):
        self._outer = outer

    async def get_positions(self, account_names=None, connector_names=None, limit=50):
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": [dict(row) for row in self._outer.positions]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ):
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": [dict(row) for row in self._outer.orders]}

    async def search_orders(self, **kwargs):
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": []}

    async def get_position_mode(self, account_name, connector_name):
        return {"position_mode": "HEDGE"}


class FakeExecutors:
    def __init__(self, outer):
        self._outer = outer

    async def search_executors(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        controller_ids=None,
        limit=50,
    ):
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": [dict(row) for row in self._outer.executor_rows]}


class FakePortfolio:
    def __init__(self, outer):
        self._outer = outer

    async def get_state(
        self, account_names=None, connector_names=None, skip_gateway=False
    ):
        return json.loads(json.dumps(self._outer.portfolio_state))


class FakeConnectors:
    def __init__(self, outer):
        self._outer = outer

    async def get_trading_rules(self, connector_name, trading_pairs=None):
        return json.loads(json.dumps(self._outer.rules))


class FakeClient:
    def __init__(self):
        self.fail_venue = False
        self.prices = {SYMBOL: "50000"}
        self.positions = [
            {
                "position_id": "p1",
                "trading_pair": SYMBOL,
                "position_side": "LONG",
                "net_amount_base": "0.1",
                "current_price": "50000",
            }
        ]
        self.executor_rows = [
            {"executor_id": "ex-1", "status": "RUNNING", "trading_pair": SYMBOL}
        ]
        self.orders = []
        self.portfolio_state = {
            "acct": {
                "binance_perpetual": [
                    {"token": "USDT", "value": 1000.0},
                    {"token": "BTC", "value": 100.0},
                ]
            }
        }
        self.rules = {
            "trading_rules": {
                SYMBOL: {
                    "min_base_amount_increment": "0.001",
                    "min_order_size": "0.001",
                    "min_notional": "10",
                    "max_leverage": 10,
                }
            }
        }
        self.market_data = FakeMarketData(self)
        self.trading = FakeTrading(self)
        self.executors = FakeExecutors(self)
        self.portfolio = FakePortfolio(self)
        self.connectors = FakeConnectors(self)


def no_trade_intent():
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "NO_TRADE",
        "symbol": SYMBOL,
        "decision_time_ms": DECISION_MS,
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


def market_context_doc():
    return {
        "schema": "brooks.market-context.v1",
        "role": "HTF_ANALYST",
        "symbol": SYMBOL,
        "decision_time_ms": DECISION_MS,
        "timeframe": "D1",
        "observations": ["range"],
        "evidence_against": ["bull closes"],
        "uncertainty": ["breakout unknown"],
    }


def write_production_state(root):
    trades = root / "trades" / "trade-1"
    trades.mkdir(parents=True)
    (trades / "binding.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": "trade-1",
                "account_name": "acct",
                "connector_name": "binance_perpetual",
                "controller_id": "ctrl",
                "symbol": SYMBOL,
                "main_position_id": "p1",
                "main_executor_id": "ex-1",
                "main_side": "LONG",
                "status": "submitted",
            }
        )
    )
    (trades / "original_trade_intent.json").write_text(json.dumps(no_trade_intent()))
    brooks_state = root / "brooks_state"
    (brooks_state / "trader").mkdir(parents=True)
    (brooks_state / "trader" / "latest.json").write_text(json.dumps(no_trade_intent()))
    (brooks_state / "htf").mkdir(parents=True)
    (brooks_state / "htf" / "latest.json").write_text(json.dumps(market_context_doc()))
    history_dir = brooks_state / "trades" / "trade-1"
    history_dir.mkdir(parents=True)
    (history_dir / "management_history.jsonl").write_text(
        '{"action": "HOLD", "decision_time_ms": 1000}\n'
    )


def load_factory(tmp_path, fake=None, **overrides):
    from condor.brooks.adapters import build_pm_load_context

    params = {
        "account_name": "acct",
        "connector_name": "binance_perpetual",
        "controller_id": "ctrl",
        "now_fn": lambda: 2_000,
    }
    params.update(overrides)
    return build_pm_load_context(
        fake or FakeClient(),
        account_name=params["account_name"],
        connector_name=params["connector_name"],
        controller_id=params["controller_id"],
        state_root=tmp_path,
        now_fn=params["now_fn"],
    )


def test_pm_context_happy_path(tmp_path):
    write_production_state(tmp_path)
    context = asyncio.run(load_factory(tmp_path)("trade-1"))
    assert context is not None
    assert context["correlation_id"] == "trade-1"
    assert context["symbol"] == SYMBOL
    assert context["decision_time_ms"] == 2_000
    assert context["positions"][0]["position_id"] == "p1"
    assert context["positions"][0]["ownership_role"] == "MAIN"
    assert context["position"]["position_id"] == "p1"
    assert context["hedge_state"]["structure_status"] == "single_main"
    assert context["hedge_state"]["main_position_id"] == "p1"
    assert context["margin_health"] == "SAFE"
    assert context["original_trade_intent"]["decision"] == "NO_TRADE"
    assert context["latest_trader_intent"]["symbol"] == SYMBOL
    assert context["latest_market_context"]["timeframe"] == "D1"
    assert context["management_history"] != []
    assert context["management_policy"]["policy_id"] == "brooks-pm-v1:ctrl"
    assert context["market_analysis"] is None
    assert context["recent_fills"] == [] and context["fills_since_last_event"] == []
    # No candles in the initial context; the allowlist shape is preserved.
    assert "candles" not in context and "bars" not in context and "ohlc" not in context
    assert set(context) <= {
        "correlation_id",
        "symbol",
        "decision_time_ms",
        "account",
        "position",
        "positions",
        "executor",
        "executor_state",
        "open_orders",
        "recent_fills",
        "fills_since_last_event",
        "pnl",
        "costs",
        "original_trade_intent",
        "latest_trader_intent",
        "latest_market_context",
        "management_history",
        "management_policy",
        "hedge_state",
        "margin_health",
        "market_analysis",
    }
    assert context["executor_state"]["executors"][0]["executor_id"] == "ex-1"


@pytest.mark.asyncio
async def test_pm_context_fails_closed(tmp_path):
    write_production_state(tmp_path)
    load = load_factory(tmp_path)
    assert await load("no-such-trade") is None
    assert await load("trade-1;DROP") is None
    assert await load("") is None

    other_controller = load_factory(tmp_path, controller_id="someone-else")
    assert await other_controller("trade-1") is None

    missing_main = FakeClient()
    missing_main.positions = []
    assert await load_factory(tmp_path, fake=missing_main)("trade-1") is None

    venue_down = FakeClient()
    venue_down.fail_venue = True
    assert await load_factory(tmp_path, fake=venue_down)("trade-1") is None

    torn = tmp_path / "trades" / "trade-1" / "original_trade_intent.json"
    torn.write_text('{"schema": "nope"}')
    assert await load("trade-1") is None


def test_pm_context_open_orders_strict_and_ambient_lenient(tmp_path):
    write_production_state(tmp_path)
    fake = FakeClient()
    fake.orders = [
        {
            "client_order_id": "o-1",
            "trading_pair": SYMBOL,
            "side": "SELL",
            "type": "STOP",
            "status": "OPEN",
            "amount": "0.05",
            "filled_amount": "0",
        },
        {"trading_pair": SYMBOL, "side": "BUY"},
    ]
    assert asyncio.run(load_factory(tmp_path, fake=fake)("trade-1")) is None

    fake.orders = [
        {
            "client_order_id": "o-1",
            "trading_pair": SYMBOL,
            "side": "SELL",
            "type": "STOP",
            "status": "OPEN",
            "amount": "0.05",
        }
    ]
    context = asyncio.run(load_factory(tmp_path, fake=fake)("trade-1"))
    assert context["open_orders"][0]["order_id"] == "o-1"
    assert context["open_orders"][0]["side"] == "SHORT"
    assert "filled_quantity" not in context["open_orders"][0]

    (tmp_path / "brooks_state" / "htf" / "latest.json").write_text("corrupt{")
    degraded = asyncio.run(load_factory(tmp_path)("trade-1"))
    assert degraded is not None
    assert degraded["latest_market_context"] is None


def test_wire_supervisor_attaches_production_pm_context(tmp_path):
    from condor.brooks.adapters import wire_supervisor

    attached = {}

    class StubSupervisor:
        def attach_symbols(self, symbols):
            attached["symbols"] = list(symbols)

        def attach_candle_source(self, source):
            attached["candle_source"] = source

        def attach_agent_key(self, agent_key, user_id=None):
            attached["agent_key"] = agent_key

        def attach_watcher_snapshots(self, provider):
            attached["watcher"] = provider

        def attach_gm_factory(self, factory):
            attached["gm_factory"] = factory

        def attach_pm(self, **kwargs):
            attached["pm"] = kwargs

    async def exercise():
        fake = FakeClient()

        async def get_client():
            return fake

        return await wire_supervisor(
            StubSupervisor(),
            {
                "brooks": {
                    "symbols": [SYMBOL],
                    "account_name": "acct",
                    "connector_name": "binance_perpetual",
                }
            },
            strategy_home=tmp_path,
            agent_key="test-key",
            user_id=1,
            agent_id="ctrl",
            get_client=get_client,
        )

    result = asyncio.run(exercise())
    assert result.ok
    assert callable(attached["pm"]["load_context"])
    write_production_state(tmp_path)
    context = asyncio.run(attached["pm"]["load_context"]("trade-1"))
    assert context is not None
    assert context["symbol"] == SYMBOL
    assert context["hedge_state"]["structure_status"] == "single_main"
