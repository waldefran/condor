"""Pending activation reaches existing GM, reconciliation and real PM loader."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from condor.brooks import gm as gm_module
from condor.brooks.contracts import TradeIntentV2
from condor.brooks.adapters import build_watcher_provider
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.gm import BrooksGM, GMPolicy
from condor.brooks.pm import PositionManager
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.store import BrooksStore
from condor.brooks.supervisor import GMConsumer
from scripts.brooks_walkforward import WalkForward
from scripts.brooks_walkforward_simulation import WalkForwardVenueAdapter
from tests.brooks_e2e_harness import SYMBOL, hold_decision, make_entry_intent


@pytest.mark.asyncio
async def test_confirmed_pending_entry_opens_once_and_pm_receives_original(tmp_path, monkeypatch):
    runner = object.__new__(WalkForward)
    runner.root = tmp_path / "output"
    runner.root.mkdir()
    runner.store = BrooksStore(tmp_path / "state")
    runner.events = EventBus(runner.store)
    runner.event_queue = runner.events.subscribe()
    runner.pending_entries, runner.pending_events = {}, {}
    runner.recorded_run = None
    runner.gm_results = {}
    runner.now = 1789862400000
    runner.venue = WalkForwardVenueAdapter(SYMBOL, state_root=runner.store.root,
        starting_mark="119", now_fn=lambda: runner.now)
    runner.venue.now_ms = runner.now
    runner.policy = GMPolicy(risk_per_trade_pct=Decimal("0.0005"), max_positions=1,
        max_gross_exposure_pct=Decimal("1"), leverage=5, take_profit_r=Decimal("2"),
        time_limit_sec=86400, max_trigger_drift_pct=Decimal("0.02"))
    monkeypatch.setattr(gm_module, "time", SimpleNamespace(time=lambda: runner.now / 1000))
    gate = BrooksGM(account_name=runner.venue.account_name,
        connector_name=runner.venue.connector_name, state_root=runner.store.root,
        policy=runner.policy, reader=runner.venue.reader, execution=runner.venue.execution,
        reconciler=runner.venue.reconciler)
    runner.gm_consumer = GMConsumer(gm_factory=lambda symbol: gate, publish=runner.events)
    original = make_entry_intent(runner.now - 1)
    original["setup"]["trigger_status"] = "pending"
    original["trigger"]["kind"] = "stop"
    original["trigger"]["direction"] = "above"
    original = TradeIntentV2.model_validate(original).model_dump(mode="json")
    runner.store.save_trader_intent(original)
    event = BrooksEvent(EventType.TRADER_INTENT_CREATED, SYMBOL,
        {"intent": original}, correlation_id="pending-test")
    runner._register_pending_entry(event)
    assert not runner.venue.trades
    bar = {"open_time_ms": runner.now, "close_time_ms": runner.now + 59999,
        "open": "119", "high": "122", "low": "118", "close": "121"}
    runner.start = runner.now
    runner.end = runner.start + 180000
    runner.minute_index = 0
    runner.minute_bars = [bar, {"open_time_ms": runner.now + 60000,
        "close_time_ms": runner.now + 119999, "open": "121", "high": "122",
        "low": "99", "close": "100"}]
    watcher = PositionWatcher(build_watcher_provider(runner.venue.client,
        account_name=runner.venue.account_name, connector_name=runner.venue.connector_name,
        controller_id=runner.venue.controller_id, symbols=[SYMBOL], state_root=runner.store.root),
        runner.events, on_snapshot=runner.gm_consumer.reconcile_bound_snapshot)
    runner.watcher = watcher
    await runner._advance_locked(runner.start + 120000, stop_on_event=True)
    assert runner.now == bar["close_time_ms"]
    assert runner.minute_index == 1  # Opening wake precedes the next candle's stop.
    await runner._activate_pending_entries(bar)
    assert len(runner.venue.trades) == 1
    assert runner.gm_results["pending-test"]["type"] == "GM_ENTRY_APPROVED"
    trade_dir = runner.store.root / "trades" / "pending-test"
    assert json.loads((trade_dir / "original_trade_intent.json").read_text()) == original
    assert json.loads((trade_dir / "execution_trade_intent.json").read_text())["setup"]["trigger_status"] == "triggered"
    assert original["setup"]["trigger_status"] == "pending"
    await watcher.poll()
    assert EventType.POSITION_OPENED in {e.type for e in runner.store.read_events()}

    async def pm_role(role, **kwargs):
        prompt = kwargs["prompt"]
        assert prompt["original_trade_intent"] == original
        assert prompt["position"]["entry_price"] == str(runner.venue.trades[0].entry_price)
        assert len(prompt["recent_fills"]) == 1
        return hold_decision(prompt["decision_time_ms"], prompt["position"]["position_id"])

    pm = PositionManager(runner=pm_role, load_context=runner.venue.pm_load_context,
        save_decision=runner.save_pm, publish=runner.events,
        candle_source=None, record_market_read=runner.record_pm_read)
    result = await pm.handle_event({"type": "PM_TIMER", "correlation_id": "pending-test"})
    assert result.action == "HOLD"
    assert EventType.MANAGEMENT_INTENT_CREATED in {e.type for e in runner.store.read_events()}
    assert len(runner.venue.trades) == 1
    runner.events.close()
    runner.store.flush()
