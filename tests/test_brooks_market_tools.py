"""Closed-bar and read-only role boundary tests with an injected market source."""

import asyncio
from copy import deepcopy

import pytest

from condor.brooks.contracts import PositionManagementInputV2
from condor.brooks.market_tools import ClosedBarError, ClosedBarGate, PMMarketTools, TraderMarketTools


HOUR = 3_600_000


def bars(count=3):
    return [
        {"open_time_ms": i * HOUR, "close_time_ms": (i + 1) * HOUR - 1,
         "closed": True, "open": "100", "high": "110", "low": "90", "close": "105",
         "volume": "10", "account": {"equity": "private"}}
        for i in range(count)
    ]


class Source:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return deepcopy(self.rows)


def snapshot():
    return PositionManagementInputV2.model_validate({
        "schema": "brooks.position-management-input.v2", "role": "POSITION_MANAGER",
        "decision_time_ms": 3 * HOUR - 1,
        "account": {"balance": "1000", "equity": "1000", "available_margin": "500", "fees": "0", "funding": "0", "currency": "USDT", "leverage": "1", "position_mode": "HEDGE"},
        "positions": [], "open_orders": [], "fills_since_last_event": [], "management_history": [],
        "market_analysis": None,
        "management_policy": {"policy_id": "p1", "version": "1", "policy_family": "test", "strategy_stop_required": True, "allowed_management_actions": ["HOLD"], "protection_semantics": "initial stop"},
        "hedge_state": {"main_side": None, "main_size": "0", "hedge_side": None, "hedge_size": "0", "net_exposure": "0", "hedge_ratio": "0", "main_position_id": None, "hedge_position_id": None, "unresolved": False, "structure_status": "no_positions", "net_exposure_usd": "0", "gross_exposure_usd": "0"},
        "margin_health": "SAFE",
    })


def test_closed_bar_gate_returns_exact_window_and_strips_private_data():
    result = ClosedBarGate(timeframe="1h", decision_time_ms=3 * HOUR - 1).validate(bars(), required_count=2, trigger_timeframe=True)
    assert [bar.open_time_ms for bar in result] == [HOUR, 2 * HOUR]
    assert "account" not in result[-1].model_dump()


@pytest.mark.parametrize("mutate,code", [
    (lambda rows: rows[1].update(closed=False), "FORMING_BAR_DETECTED"),
    (lambda rows: rows[1].pop("open_time_ms"), "BAR_ORDER_INVALID"),
    (lambda rows: rows[1].update(close_time_ms=2 * HOUR), "FORMING_BAR_DETECTED"),
    (lambda rows: rows[1].update(open_time_ms=0, close_time_ms=HOUR - 1), "BAR_ORDER_INVALID"),
    (lambda rows: rows[1].update(open_time_ms=3 * HOUR, close_time_ms=4 * HOUR - 1), "BAR_GAP"),
    (lambda rows: rows[1].update(high="99"), "TRADER_INPUT_INVALID"),
    (lambda rows: rows[1].update(close="NaN"), "TRADER_INPUT_INVALID"),
])
def test_gate_rejects_forming_duplicate_gap_and_bad_ohlc(mutate, code):
    rows = bars(2)
    mutate(rows)
    with pytest.raises(ClosedBarError) as exc:
        ClosedBarGate(timeframe="H1", decision_time_ms=4 * HOUR - 1).validate(rows, required_count=2)
    assert exc.value.code == code


def test_future_bar_is_excluded_and_minimum_count_enforced():
    gate = ClosedBarGate(timeframe="1h", decision_time_ms=2 * HOUR - 1)
    assert len(gate.validate(bars(3), required_count=2)) == 2
    with pytest.raises(ClosedBarError, match="closed bars") as exc:
        gate.validate(bars(3), required_count=3)
    assert exc.value.code == "INSUFFICIENT_HISTORY"


def test_trigger_bar_must_close_at_decision_time():
    with pytest.raises(ClosedBarError) as exc:
        ClosedBarGate(timeframe="1h", decision_time_ms=4 * HOUR - 1).validate(bars(3), required_count=2, trigger_timeframe=True)
    assert exc.value.code == "BAR_ORDER_INVALID"


def test_trader_tools_are_market_only_and_bounded():
    asyncio.run(_test_trader_tools_are_market_only_and_bounded())


async def _test_trader_tools_are_market_only_and_bounded():
    source = Source(bars())
    tools = TraderMarketTools(source=source, decision_time_ms=3 * HOUR - 1)
    assert len(await tools.get_closed_candles("BTC-USDT", "H1", 3)) == 3
    assert source.calls == [("BTC-USDT", "1h", 4)]
    structure = await tools.get_recent_structure("BTC-USDT", "1h", window=3)
    assert structure["range_high"] == "110"
    assert (await tools.get_volatility("BTC-USDT", "1h", window=3))["mean_high_low_range"] == "20"
    assert not hasattr(tools, "get_position_state")
    assert not hasattr(tools, "create_order_executor")
    with pytest.raises(ValueError):
        await tools.get_closed_candles("BTC-USDT", "1h", 121)
    with pytest.raises(ClosedBarError):
        await tools.get_closed_candles("BTC-USDT", "1m", 1)


def test_pm_candle_limit_and_snapshot_tools():
    asyncio.run(_test_pm_candle_limit_and_snapshot_tools())


async def _test_pm_candle_limit_and_snapshot_tools():
    source = Source(bars())
    tools = PMMarketTools(source=source, snapshot=snapshot(), symbol="BTC-USDT", executor_state={"status": "running"})
    assert len(await tools.get_candles("BTC-USDT", "1h", 3)) == 3
    assert tools.get_position_state() == []
    assert tools.get_open_orders() == []
    assert tools.get_recent_fills() == []
    state = tools.get_executor_state()
    state["status"] = "changed"
    assert tools.get_executor_state()["status"] == "running"
    with pytest.raises(ValueError):
        await tools.get_candles("BTC-USDT", "1h", 31)
    with pytest.raises(ValueError):
        await tools.get_candles("ETH-USDT", "1h", 1)
    with pytest.raises(ValueError):
        await tools.get_recent_structure("ETH-USDT", "1h")
    with pytest.raises(ValueError):
        await tools.get_recent_structure("BTC-USDT", "1h", window=31)
    assert source.calls == [("BTC-USDT", "1h", 4)]


def test_pm_rejects_future_snapshot_data():
    data = snapshot().model_dump()
    data["positions"] = [{
        "position_id": "main-1", "symbol": "BTC-USDT", "side": "LONG", "quantity": "1",
        "entry_price": "100", "mark_price": "101", "unrealized_pnl": "1",
        "protective_order_ids": [], "ownership_role": "MAIN", "as_of_ms": 4 * HOUR,
    }]
    with pytest.raises(ValueError, match="future position"):
        PMMarketTools(source=Source(bars()), snapshot=PositionManagementInputV2.model_validate(data), symbol="BTC-USDT")
