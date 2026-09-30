"""Exercise the offline venue with the production lifecycle and PM adapters."""

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from condor.brooks import gm as gm_module
from condor.brooks.adapters import build_watcher_provider, read_bindings
from condor.brooks.contracts import TradeIntentV2
from condor.brooks.events import EventBus, EventType
from condor.brooks.gm import BrooksGM, GMPolicy
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.store import BrooksStore
from condor.brooks.supervisor import GMConsumer
from scripts.brooks_walkforward_simulation import WalkForwardVenueAdapter
from tests.brooks_e2e_harness import SYMBOL, make_entry_intent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["STOP_LOSS", "TAKE_PROFIT", "TIME_LIMIT", "PM_CLOSE"]
)
async def test_closed_simulated_main_releases_binding_and_next_entry(
    tmp_path, monkeypatch, reason
):
    store = BrooksStore(tmp_path)
    bus = EventBus(store)
    venue = WalkForwardVenueAdapter(SYMBOL, state_root=store.root, starting_mark="120")
    start = (int(time.time() * 1000) // 60_000) * 60_000
    venue.now_ms = start
    monkeypatch.setattr(
        gm_module, "time", SimpleNamespace(time=lambda: venue.now_ms / 1000)
    )
    policy = GMPolicy(Decimal("0.001"), 2, Decimal("2"), 2, Decimal("2"), 60)
    gate = BrooksGM(
        account_name=venue.account_name,
        connector_name=venue.connector_name,
        state_root=store.root,
        policy=policy,
        reader=venue.reader,
        execution=venue.execution,
        reconciler=venue.reconciler,
    )
    consumer = GMConsumer(gm_factory=lambda symbol: gate, publish=bus)
    watcher = PositionWatcher(
        build_watcher_provider(
            venue.client,
            account_name=venue.account_name,
            connector_name=venue.connector_name,
            controller_id=venue.controller_id,
            symbols=[SYMBOL],
            state_root=store.root,
        ),
        bus,
        on_snapshot=consumer.reconcile_bound_snapshot,
    )

    original = TradeIntentV2.model_validate(make_entry_intent(start - 1))
    store.save_trader_intent(original.model_dump(mode="json"))
    venue.stage_trade_intent("first", original)
    opened = await gate.execute_entry(original, correlation_id="first")
    assert opened["status"] == "reconciled"
    await watcher.poll()
    pm = await venue.pm_load_context("first")
    assert pm["latest_trader_intent"] == original.model_dump(mode="json")
    assert pm["position"]["entry_price"] == str(venue.trades[0].entry_price)
    assert pm["executor_state"]["fills_read_status"] == "available"
    assert len(pm["recent_fills"]) == 1
    assert Decimal(pm["recent_fills"][0]["fee"]) == venue.fees_paid
    bound_executor = pm["executor_state"]["executors"][0]
    assert Decimal(bound_executor["config"]["stop_price"]) == Decimal("100")
    assert Decimal(bound_executor["config"]["target_price"]) == Decimal("160")

    if reason == "PM_CLOSE":
        await gate.execute_management(
            correlation_id="first", decision_id="close", action="CLOSE"
        )
    else:
        high, low = {
            "STOP_LOSS": ("121", "99"),
            "TAKE_PROFIT": ("161", "119"),
            "TIME_LIMIT": ("121", "119"),
        }[reason]
        # TIME_LIMIT needs the next complete minute, after the configured 60s.
        for offset in (0, 60_000) if reason == "TIME_LIMIT" else (0,):
            bar = {
                "open_time_ms": start + offset,
                "close_time_ms": start + offset + 59_999,
                "open": "120",
                "high": high,
                "low": low,
                "close": "120",
                "closed": True,
            }
            venue.resolve_executor_bar(bar)
    await watcher.poll()
    binding = json.loads((store.root / "trades" / "first" / "binding.json").read_text())
    assert binding["status"] == "closed"
    assert binding["closed_main_position_id"] == opened["main_position_id"]
    assert (
        read_bindings(
            store.root,
            account_name=venue.account_name,
            connector_name=venue.connector_name,
            controller_id=venue.controller_id,
        )
        == []
    )
    assert EventType.POSITION_CLOSED in {e.type for e in store.read_events()}
    assert EventType.BINDING_CLOSED in {e.type for e in store.read_events()}
    if reason != "PM_CLOSE":
        assert venue.trades[0].close_reason == reason

    venue.set_market(mark_price="120", decision_time_ms=venue.now_ms)
    next_intent = TradeIntentV2.model_validate(make_entry_intent(start - 1))
    venue.stage_trade_intent("second", next_intent)
    second = await gate.execute_entry(next_intent, correlation_id="second")
    assert second["status"] == "reconciled"
    assert second["main_position_id"] != opened["main_position_id"]
    assert len(venue.trades) == 2
