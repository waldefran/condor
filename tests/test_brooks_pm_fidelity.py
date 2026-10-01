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
from condor.brooks.pm import PositionManager

SYMBOL = "BTC-USDT"
ACCOUNT = "acct"
CONNECTOR = "binance_perpetual"
CONTROLLER = "ctrl"
NOW_MS = 1_800_000_000_000
HOUR_MS = 3_600_000
H4_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS


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


def _context_v2(timeframe, decision_time_ms, *, symbol=SYMBOL, **changes):
    value = {
        "schema": "brooks.market-context.v2",
        "role": "CONTEXT_ANALYST",
        "symbol": symbol,
        "timeframe": timeframe,
        "decision_time_ms": decision_time_ms,
        "window_bars": 120,
        "primary_regime": "trading-range",
        "phase": "range",
        "breakout_mode": "unclear",
        "directional_pressure": "balanced",
        "always_in": "unclear",
        "always_in_relevance": "low",
        "observations": [f"V2 {timeframe} range"],
        "structures": [],
        "evidence_for": [f"{timeframe} supporting evidence"],
        "evidence_against": [f"{timeframe} opposing evidence"],
        "transition_conditions": [f"{timeframe} transition condition"],
        "missing_information": [f"{timeframe} missing information"],
        "confidence": "medium",
    }
    value.update(changes)
    return value


def _write_macro_context(root, timeframe, context):
    path = root / "brooks_state" / "context" / timeframe.lower() / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context))


def _write_latest_trader(root, *, decision_time_ms, symbol=SYMBOL):
    intent = _intent()
    intent["symbol"] = symbol
    intent["decision_time_ms"] = decision_time_ms
    (root / "brooks_state" / "trader" / "latest.json").write_text(
        json.dumps(intent)
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


def test_pm_macro_bundle_reaches_first_prompt_and_market_tool_frozen(tmp_path):
    _write_state(tmp_path)
    # Pick a D1 close on the absolute Unix epoch grid. The same instant is
    # also an H4 and H1 close, so all three currentness checks agree.
    pm_time = ((NOW_MS + 1) // DAY_MS) * DAY_MS - 1 + DAY_MS
    expected_d1_time = ((pm_time + 1) // DAY_MS) * DAY_MS - 1
    expected_h4_time = ((pm_time + 1) // H4_MS) * H4_MS - 1
    assert expected_d1_time == expected_h4_time == pm_time
    d1 = _context_v2("D1", expected_d1_time)
    h4 = _context_v2(
        "H4",
        expected_h4_time,
        primary_regime="bull-trend",
        phase="channel",
        evidence_for=["H4 higher highs and strong bull closes"],
        evidence_against=["H4 is extended from its moving average"],
    )
    _write_macro_context(tmp_path, "D1", d1)
    _write_macro_context(tmp_path, "H4", h4)
    _write_latest_trader(tmp_path, decision_time_ms=pm_time)

    client = _Client()
    load = build_pm_load_context(
        client,
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        controller_id=CONTROLLER,
        state_root=tmp_path,
        now_fn=lambda: pm_time,
    )
    context = asyncio.run(load("trade-1"))
    assert context is not None
    assert context["macro_contexts"]["D1"] == {"freshness": "current", "context": d1}
    assert context["macro_contexts"]["H4"] == {"freshness": "current", "context": h4}
    assert context["latest_trader_intent"]["decision_time_ms"] == pm_time
    assert context["latest_trader_intent_freshness"] == "current"

    # Change the ambient files after loading. This wake and its read tool must
    # continue to use the exact D1/H4 packet assembled for this snapshot.
    _write_macro_context(
        tmp_path,
        "D1",
        _context_v2("D1", expected_d1_time, primary_regime="bear-trend"),
    )
    _write_latest_trader(tmp_path, decision_time_ms=pm_time + 1)

    seen = {}

    class CaptureRunner:
        async def run(self, role, **kwargs):
            seen["role"] = role
            seen["prompt"] = kwargs["prompt"]
            seen["market_context"] = await (
                kwargs["market_tools"]["get_market_context"]()
            )
            return {
                "schema": "brooks.management-decision.v2",
                "role": "POSITION_MANAGER",
                "decision_time_ms": pm_time,
                "action": "HOLD",
                "position_ids": ["p1"],
                "reason": "Maintain the bound position.",
                "evidence": {
                    "observations": ["Position remains open."],
                    "evidence_for": ["No management trigger."],
                    "evidence_against": ["Market conditions can change."],
                },
                "risk": {
                    "exposure_before": ["Long position."],
                    "exposure_after": ["Long position."],
                    "protection_status": "unknown",
                    "costs_considered": ["No execution cost."],
                    "uncertainty": "medium",
                },
                "execution": {
                    "orders": [],
                    "cancel_order_ids": [],
                    "replace_orders": [],
                },
                "hedge_plan": None,
                "market_analysis_request": None,
                "conditions_that_change_action": ["A structural change."],
            }

    manager = PositionManager(
        runner=CaptureRunner(),
        load_context=lambda correlation_id: context,
        save_decision=lambda correlation_id, decision: None,
        publish=lambda event: None,
        candle_source=lambda symbol, timeframe, limit: [],
        record_market_read=lambda correlation_id, record: None,
    )
    decision = asyncio.run(
        manager.handle_event(
            {
                "type": "POSITION_CHANGED",
                "correlation_id": "trade-1",
                "event_id": "wake-1",
            }
        )
    )

    assert decision.action == "HOLD"
    assert seen["role"] == "POSITION_MANAGER"
    prompt = seen["prompt"]
    assert prompt["macro_contexts"] == context["macro_contexts"]
    assert prompt["latest_trader_intent_freshness"] == "current"
    d1_prompt = prompt["macro_contexts"]["D1"]["context"]
    h4_prompt = prompt["macro_contexts"]["H4"]["context"]
    assert d1_prompt["primary_regime"] == "trading-range"
    assert d1_prompt["phase"] == "range"
    assert d1_prompt["evidence_for"] == d1["evidence_for"]
    assert d1_prompt["evidence_against"] == d1["evidence_against"]
    assert h4_prompt["primary_regime"] == "bull-trend"
    assert h4_prompt["phase"] == "channel"
    assert h4_prompt["evidence_for"] == h4["evidence_for"]
    assert seen["market_context"] == prompt["macro_contexts"]


def test_pm_snapshot_filters_unusable_macro_and_trader_contexts(tmp_path):
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

    stale_d1_time = ((NOW_MS + 1) // DAY_MS) * DAY_MS - 1 - DAY_MS
    _write_macro_context(tmp_path, "D1", _context_v2("D1", stale_d1_time))
    _write_latest_trader(tmp_path, decision_time_ms=NOW_MS + 1)
    stale = asyncio.run(load("trade-1"))
    assert stale is not None  # Unusable ambient context does not block MAIN.
    assert stale["macro_contexts"]["D1"]["freshness"] == "stale"
    assert stale["macro_contexts"]["D1"]["context"]["evidence_for"] == [
        "D1 supporting evidence"
    ]
    assert stale["macro_contexts"]["H4"] == {"freshness": "missing", "context": None}
    assert stale["latest_trader_intent"] is None
    assert stale["latest_trader_intent_freshness"] == "missing"

    # A same-symbol future V2 context and a current other-symbol context are
    # both excluded from this BTC snapshot; a valid ETH TraderIntent is too.
    _write_macro_context(
        tmp_path, "D1", _context_v2("D1", NOW_MS + 1, primary_regime="bear-trend")
    )
    _write_macro_context(
        tmp_path, "H4", _context_v2("H4", NOW_MS - 1, symbol="ETH-USDT")
    )
    _write_latest_trader(tmp_path, decision_time_ms=NOW_MS - 1, symbol="ETH-USDT")
    hidden = asyncio.run(load("trade-1"))
    assert hidden is not None
    assert hidden["macro_contexts"] == {
        "D1": {"freshness": "missing", "context": None},
        "H4": {"freshness": "missing", "context": None},
    }
    assert hidden["latest_trader_intent"] is None
    assert hidden["latest_trader_intent_freshness"] == "missing"


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
