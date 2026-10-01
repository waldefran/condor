"""Opt-in LONG lock behavior in the historical venue."""

from decimal import Decimal

import pytest

from condor.brooks.execution import ExecutionRejected
from scripts.brooks_walkforward_simulation import WalkForwardVenueAdapter, _Position


SYMBOL = "ETH-USDT"


async def _opened(tmp_path, side="LONG", *, locked=True):
    venue = WalkForwardVenueAdapter(SYMBOL, state_root=tmp_path,
        starting_mark="120", long_lock_policy=locked)
    venue.now_ms = 120_001
    venue.stage_trade_intent("operation", {"symbol": SYMBOL,
        "setup": {"trigger_status": "triggered"}, "trigger": {"kind": "stop"}})
    await venue.execution.open_main(symbol=SYMBOL, side=side,
        quantity=Decimal("1"), leverage=2,
        stop_loss_pct=Decimal("0.1"), take_profit_pct=Decimal("0.2"),
        time_limit_sec=60)
    return venue


def _bar(open_ms, *, low="107", high="121", close="107"):
    return {"open_time_ms": open_ms, "close_time_ms": open_ms + 59_999,
        "open": "120", "high": high, "low": low, "close": close, "closed": True}


@pytest.mark.asyncio
async def test_long_touch_requests_lock_and_negative_main_close_is_atomic(tmp_path):
    venue = await _opened(tmp_path)
    venue.resolve_executor_bar(_bar(120_000))  # partial entry minute
    assert not venue.long_lock_requests
    venue.resolve_executor_bar(_bar(180_000))
    assert len(venue.long_lock_requests) == 1
    assert venue.trades[0].is_open
    main = venue._main_position(SYMBOL)
    before = (len(venue.fills), len(venue.executor_rows), main.quantity)
    with pytest.raises(ExecutionRejected, match="negative"):
        venue._close_main_quantity(symbol=SYMBOL, side="LONG", quantity=Decimal("1"),
            reason="PM_CLOSE", executor_status="CLOSED")
    assert before == (len(venue.fills), len(venue.executor_rows), main.quantity)
    venue.resolve_executor_bar(_bar(240_000))
    assert len(venue.long_lock_requests) == 1  # disarmed until host rearms


@pytest.mark.asyncio
async def test_short_stop_is_unchanged(tmp_path):
    venue = await _opened(tmp_path, side="SHORT")
    venue.resolve_executor_bar(_bar(120_000, low="119", high="121", close="120"))
    result = venue.resolve_executor_bar(_bar(180_000, low="119", high="133", close="130"))
    assert result["reason"] == "STOP_LOSS"
    assert not venue.trades[0].is_open


@pytest.mark.asyncio
async def test_hedge_may_close_at_loss_then_profitable_main_can_close(tmp_path):
    venue = await _opened(tmp_path)
    main = venue._main_position(SYMBOL)
    hedge = _Position("hedge", SYMBOL, "SHORT", Decimal("1"), Decimal("120"),
        "HEDGE", venue.now_ms, "hedge-exec", "operation", Decimal("1"), 2)
    venue._positions[hedge.position_id] = hedge
    venue.set_market(mark_price="130", decision_time_ms=180_000)
    with pytest.raises(ExecutionRejected, match="hedge remains"):
        venue._close_position(main, main.quantity, reason="PM_CLOSE")
    venue._close_position(hedge, hedge.quantity, reason="REMOVE_HEDGE")
    assert hedge.quantity == 0
    venue.set_market(mark_price="150", decision_time_ms=240_000)
    assert Decimal(venue.long_policy_context("operation")["projected_exit_net"]) >= 0
    venue._close_position(main, main.quantity, reason="PM_CLOSE")
    assert not venue.trades[0].is_open


@pytest.mark.asyncio
async def test_lock_state_survives_checkpoint(tmp_path):
    venue = await _opened(tmp_path)
    venue.resolve_executor_bar(_bar(120_000))
    venue.resolve_executor_bar(_bar(180_000))
    checkpoint = venue.save_checkpoint(tmp_path / "checkpoint.json")
    restored = WalkForwardVenueAdapter(SYMBOL, state_root=tmp_path,
        starting_mark="120", long_lock_policy=True)
    restored.load_checkpoint(checkpoint)
    assert restored.long_lock_requests == venue.long_lock_requests
    assert restored.long_policy_context("operation") == venue.long_policy_context("operation")
    with pytest.raises(ValueError, match="config"):
        WalkForwardVenueAdapter(SYMBOL, state_root=tmp_path,
            starting_mark="120").load_checkpoint(checkpoint)
