"""Position-management snapshots retain only authoritative bound trade facts."""

from __future__ import annotations

import asyncio
import json

import pytest

from condor.brooks import agent_runner
from condor.brooks.adapters import (
    _pm_latest_context,
    _pm_latest_intent,
    build_pm_load_context,
)

SYMBOL = "BTC-USDT"
ACCOUNT = "acct"
CONNECTOR = "binance_perpetual"
CONTROLLER = "ctrl"
NOW_MS = 1_800_000_000_000


def _intent():
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "NO_TRADE",
        "symbol": SYMBOL,
        "decision_time_ms": NOW_MS - 10_000,
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
        "conditions_that_change_market_read": ["A new breakout"],
    }


def _context():
    return {
        "schema": "brooks.market-context.v1",
        "role": "HTF_ANALYST",
        "symbol": SYMBOL,
        "decision_time_ms": NOW_MS - 10_000,
        "timeframe": "D1",
        "observations": ["Daily range"],
        "evidence_against": ["Strong bull closes"],
        "uncertainty": ["Breakout unresolved"],
    }


def _write_state(root):
    trades = root / "trades" / "trade-1"
    trades.mkdir(parents=True)
    (trades / "binding.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": "trade-1",
                "account_name": ACCOUNT,
                "connector_name": CONNECTOR,
                "controller_id": CONTROLLER,
                "symbol": SYMBOL,
                "main_position_id": "p1",
                "main_executor_id": "ex-1",
                "main_side": "LONG",
                "status": "submitted",
            }
        )
    )
    (trades / "original_trade_intent.json").write_text(json.dumps(_intent()))
    (trades / "management_history.jsonl").write_text(
        json.dumps({"action": "HOLD", "decision_time_ms": NOW_MS - 1_000}) + "\n"
    )
    state = root / "brooks_state"
    (state / "trader").mkdir(parents=True)
    (state / "trader" / "latest.json").write_text(json.dumps(_intent()))
    (state / "htf").mkdir(parents=True)
    (state / "htf" / "latest.json").write_text(json.dumps(_context()))
    (state / "context" / "d1").mkdir(parents=True)
    (state / "context" / "d1" / "latest.json").write_text(
        json.dumps(
            {
                "schema": "brooks.market-context.v2",
                "role": "CONTEXT_ANALYST",
                "symbol": SYMBOL,
                "timeframe": "D1",
                "decision_time_ms": NOW_MS - 5_000,
                "window_bars": 120,
                "primary_regime": "trading-range",
                "phase": "range",
                "breakout_mode": "unclear",
                "directional_pressure": "balanced",
                "always_in": "unclear",
                "always_in_relevance": "low",
                "observations": ["V2 daily range"],
                "structures": [],
                "evidence_for": ["Repeated range reversals"],
                "evidence_against": ["A breakout may develop"],
                "transition_conditions": ["A strong close beyond the range"],
                "missing_information": ["Next session remains unknown"],
                "confidence": "medium",
            }
        )
    )


class _Trading:
    def __init__(self, owner):
        self.owner = owner

    async def get_positions(self, account_names=None, connector_names=None, limit=50):
        self.owner.position_calls.append((account_names, connector_names, limit))
        return {"data": [dict(row) for row in self.owner.positions]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ):
        return {"data": []}

    async def get_position_mode(self, account_name, connector_name):
        return {"position_mode": "HEDGE"}

    async def search_orders(self, **kwargs):
        self.owner.order_searches.append(kwargs)
        return {"data": [dict(row) for row in self.owner.filled_orders]}


class _Executors:
    def __init__(self, owner):
        self.owner = owner

    async def search_executors(self, **kwargs):
        return {"data": [dict(row) for row in self.owner.executor_rows]}


class _MarketData:
    async def get_prices(self, connector_name, trading_pairs):
        return {"prices": {SYMBOL: "105"}}


class _Portfolio:
    async def get_state(
        self, account_names=None, connector_names=None, skip_gateway=False
    ):
        return {
            ACCOUNT: {
                CONNECTOR: [{"token": "USDT", "value": "1000", "available": "900"}]
            }
        }


class _Connectors:
    async def get_trading_rules(self, connector_name, trading_pairs=None):
        return {
            "trading_rules": {
                SYMBOL: {
                    "min_base_amount_increment": "0.001",
                    "min_order_size": "0.001",
                    "min_notional": "5",
                    "max_leverage": 5,
                }
            }
        }


class _Client:
    def __init__(self):
        self.positions = [
            {
                "position_id": "p1",
                "trading_pair": SYMBOL,
                "position_side": "LONG",
                "net_amount_base": "0.1",
                "entry_price": "100.5",
                "current_price": "105",
                "unrealized_pnl": "0.45",
            },
            {
                "position_id": "unrelated-p2",
                "trading_pair": SYMBOL,
                "position_side": "SHORT",
                "net_amount_base": "0.2",
                "entry_price": "111",
                "current_price": "105",
                "unrealized_pnl": "1.2",
            },
        ]
        self.executor_rows = [
            {
                "executor_id": "ex-1",
                "status": "RUNNING",
                "trading_pair": SYMBOL,
                "controller_id": CONTROLLER,
                "position_id": "p1",
                "config": {
                    "stop_loss_pct": "0.02",
                    "take_profit_pct": "0.04",
                    "leverage": 5,
                },
                "custom_info": {
                    "realized_pnl_quote": "1.25",
                    "position_pnl_quote": "0.45",
                    "secret": "must not pass",
                },
                "cum_fees_quote": "0.08",
            },
            {
                "executor_id": "other-executor",
                "status": "RUNNING",
                "trading_pair": SYMBOL,
                "controller_id": CONTROLLER,
                "position_id": "unrelated-p2",
                "config": {"stop_loss_pct": "0.5"},
            },
        ]
        self.filled_orders = [
            {
                "client_order_id": "fill-1",
                "executor_id": "ex-1",
                "position_id": "p1",
                "trading_pair": SYMBOL,
                "status": "FILLED",
                "trade_type": "BUY",
                "filled_amount": "0.1",
                "price": "100.5",
                "timestamp_ms": NOW_MS - 500,
                "fee": "0.04",
            },
            {
                "client_order_id": "unrelated-fill",
                "executor_id": "other-executor",
                "position_id": "unrelated-p2",
                "trading_pair": SYMBOL,
                "status": "FILLED",
                "filled_amount": "0.2",
                "price": "111",
                "timestamp_ms": NOW_MS - 400,
            },
        ]
        self.position_calls = []
        self.order_searches = []
        self.trading = _Trading(self)
        self.executors = _Executors(self)
        self.market_data = _MarketData()
        self.portfolio = _Portfolio()
        self.connectors = _Connectors()


def test_latest_ambient_documents_accept_both_store_roots(tmp_path):
    _write_state(tmp_path)
    state_root = tmp_path / "brooks_state"

    for root in (tmp_path, state_root):
        assert _pm_latest_intent(root, "trader", SYMBOL)["symbol"] == SYMBOL
        context = _pm_latest_context(root, SYMBOL)
        assert context["timeframe"] == "D1"
        assert context["decision_time_ms"] == NOW_MS - 5_000
        assert context["observations"] == ["V2 daily range"]


def test_pm_snapshot_keeps_only_bound_position_executor_and_fills(tmp_path):
    _write_state(tmp_path)
    client = _Client()
    load = build_pm_load_context(
        client,
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        controller_id=CONTROLLER,
        state_root=tmp_path,
        now_fn=lambda: NOW_MS,
    )

    context = asyncio.run(load("trade-1"))

    assert context is not None
    assert context["position"]["entry_price"] == "100.5"
    assert context["position"]["unrealized_pnl"] == "0.45"
    assert len(client.position_calls) == 1  # facts came from the reader's same read
    assert [row["position_id"] for row in context["positions"]] == ["p1"]
    assert [row["executor_id"] for row in context["executor_state"]["executors"]] == [
        "ex-1"
    ]
    executor = context["executor_state"]["executors"][0]
    assert executor["config"] == {"stop_loss_pct": "0.02", "take_profit_pct": "0.04"}
    assert executor["custom_info"] == {
        "realized_pnl_quote": "1.25",
        "position_pnl_quote": "0.45",
    }
    assert executor["cum_fees_quote"] == "0.08"
    assert "secret" not in json.dumps(executor)
    assert [fill["fill_id"] for fill in context["recent_fills"]] == ["fill-1"]
    assert [fill["fill_id"] for fill in context["fills_since_last_event"]] == ["fill-1"]
    fill = context["recent_fills"][0]
    assert fill["position_id"] == "p1"
    assert fill["side"] == "LONG"
    assert fill["quantity"] == "0.1"
    assert fill["price"] == "100.5"
    assert fill["fee"] == "0.04"
    assert "funding" not in fill
    assert client.order_searches[0]["account_names"] == [ACCOUNT]
    assert client.order_searches[0]["connector_names"] == [CONNECTOR]
    assert client.order_searches[0]["trading_pairs"] == [SYMBOL]
    assert client.order_searches[0]["start_time"] is None
    assert client.order_searches[0]["end_time"] == NOW_MS // 1000


@pytest.mark.parametrize("failure_mode", ["missing", "raises", "incomplete"])
def test_pm_snapshot_degrades_unavailable_fills_only(tmp_path, failure_mode):
    _write_state(tmp_path)
    client = _Client()
    if failure_mode == "missing":
        client.trading.search_orders = None
    elif failure_mode == "raises":

        async def search_orders(**_kwargs):
            raise TimeoutError("optional fills endpoint unavailable")

        client.trading.search_orders = search_orders
    else:

        async def search_orders(**_kwargs):
            return {"data": [], "pagination": {"next_cursor": "more"}}

        client.trading.search_orders = search_orders

    context = asyncio.run(
        build_pm_load_context(
            client,
            account_name=ACCOUNT,
            connector_name=CONNECTOR,
            controller_id=CONTROLLER,
            state_root=tmp_path,
            now_fn=lambda: NOW_MS,
        )("trade-1")
    )

    assert context is not None
    assert context["position"]["position_id"] == "p1"
    assert context["executor_state"]["executors"][0]["executor_id"] == "ex-1"
    assert context["recent_fills"] == []
    assert context["fills_since_last_event"] == []
    assert context["executor_state"]["fills_read_status"] == "unavailable"


@pytest.mark.parametrize("timestamp", [NOW_MS - 2_000, NOW_MS + 1])
def test_pm_fills_keep_prior_costs_but_exclude_future_fills(tmp_path, timestamp):
    _write_state(tmp_path)
    client = _Client()
    client.filled_orders[0]["timestamp_ms"] = timestamp
    context = asyncio.run(
        build_pm_load_context(
            client,
            account_name=ACCOUNT,
            connector_name=CONNECTOR,
            controller_id=CONTROLLER,
            state_root=tmp_path,
            now_fn=lambda: NOW_MS,
        )("trade-1")
    )
    assert context["fills_since_last_event"] == []
    if timestamp < NOW_MS:
        assert context["recent_fills"][0]["fee"] == "0.04"
    else:
        assert context["recent_fills"] == []


def test_pm_sanitizers_do_not_fill_absent_facts_with_zero(tmp_path):
    _write_state(tmp_path)
    client = _Client()
    client.positions[0].pop("entry_price")
    client.positions[0].pop("unrealized_pnl")
    client.filled_orders[0].pop("fee")
    context = asyncio.run(
        build_pm_load_context(
            client,
            account_name=ACCOUNT,
            connector_name=CONNECTOR,
            controller_id=CONTROLLER,
            state_root=tmp_path,
            now_fn=lambda: NOW_MS,
        )("trade-1")
    )

    assert context is not None
    assert "entry_price" not in context["position"]
    assert "unrealized_pnl" not in context["position"]
    assert "fee" not in context["recent_fills"][0]


def test_default_fresh_analyst_uses_the_existing_read_only_role_tools(monkeypatch):
    from condor.brooks.contracts import TradeIntentV2
    from condor.brooks.supervisor import GMConsumer

    seen = {}

    async def fake_run_role(role, prompt, output_model, market_tools, **kwargs):
        seen.update(
            role=role, prompt=prompt, output_model=output_model, tools=market_tools
        )
        return TradeIntentV2.model_validate(_intent())

    class CandleSource:
        async def fetch_candles(self, symbol, timeframe, limit):
            raise AssertionError("fresh-analysis test must not need a candle read")

    monkeypatch.setattr(agent_runner, "run_role", fake_run_role)
    consumer = GMConsumer(
        gm_factory=None,
        publish=lambda event: None,
        agent_key="test-key",
        candle_source=CandleSource(),
    )
    run = consumer._build_default_analyst_runner(SYMBOL)
    result = asyncio.run(
        run(
            {
                "symbol": SYMBOL,
                "decision_time_ms": NOW_MS,
                "timeframes": ["1h"],
                "market_fields": ["ordered_ohlc", "decision_time"],
            }
        )
    )

    assert result.symbol == SYMBOL
    assert seen["role"] == "TRADER"
    assert set(seen["tools"]) == {
        "get_closed_candles",
        "get_recent_structure",
        "get_volatility",
    }
    assert all(callable(tool) for tool in seen["tools"].values())
