"""Wave 3 Brooks E2E, part 1: shadow, MAIN entry, PM HOLD/REDUCE, fail-closed orders.

Every test traverses the REAL production classes (MarketClock, EventBus,
BrooksStore, TraderConsumer, GMConsumer, BrooksGM, HummingbotAccountReader,
HummingbotPositionReconciler, PositionWatcher, PositionManager, run_role).
Only the Hummingbot client + ExecutionPort are faked (FakeVenue + SimPort in
brooks_e2e_harness) and only the LLM client behind run_role is mocked
(scripted FakeLLMClient). No condor/ production code is touched.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from condor.brooks.events import BrooksEvent, EventType
from condor.brooks.gm import GMRejected
from tests.brooks_e2e_harness import (
    SYMBOL,
    D,
    E2EWorld,
    FakeCandles,
    h1_due_now,
    hold_decision,
    make_entry_intent,
    open_main_position,
    read_binding,
    reduce_decision,
    run,
    script_llm,
)


def test_shadow_h1_close_persists_intent_with_zero_port_writes(tmp_path, monkeypatch):
    """Scenario 1 (SHADOW): H1 close -> ENTER_LONG in shadow mode.

    The TradeIntent is persisted, GMConsumer stays silent, and the
    ExecutionPort sees ZERO writes (no trade directory is even created).
    """
    world = E2EWorld(tmp_path)
    due = h1_due_now()
    candles = FakeCandles(due)
    fake_llm = script_llm(monkeypatch, [
        json.dumps({"tool": "get_closed_candles", "arguments": {
            "symbol": SYMBOL, "timeframe": "4h", "limit": 120,
        }}),
        json.dumps(make_entry_intent(due)),
    ])
    trader_q = world.bus.subscribe({EventType.TRADER_INTENT_CREATED})

    h1 = run(
        world.clock(candles).publish_closed_bar(
            SYMBOL, "1h", due, EventType.H1_BAR_CLOSED
        )
    )
    assert h1.correlation_id == f"{SYMBOL}-1h-{due}"

    intent = run(world.trader(candles, shadow_mode=True).handle(h1))
    assert intent.decision == "ENTER_LONG"
    assert fake_llm.started and fake_llm.stopped and len(fake_llm.prompts) == 2
    assert "Read tool get_closed_candles result" in fake_llm.prompts[1]

    latest = world.store.read_latest("trader")
    assert latest is not None and latest["decision"] == "ENTER_LONG"
    assert latest["symbol"] == SYMBOL and latest["decision_time_ms"] == due

    (trader_event,) = world.drain(trader_q)
    assert trader_event.payload["shadow_mode"] is True

    assert run(world.gm_consumer.handle(trader_event)) is None
    assert world.port.calls == []
    assert not (world.root / "trades").exists()


def test_main_h1_close_to_position_opened_with_null_to_reconciled_binding(
    tmp_path, monkeypatch
):
    """Scenario 2 (MAIN): H1 close -> ENTER_LONG -> GM -> MAIN executor.

    The venue position appears only after a lag: the binding starts with
    main_position_id = null, reconciliation stays submitted, then completes
    once the venue shows the executor's position, and the watcher publishes
    POSITION_OPENED.
    """
    world = E2EWorld(tmp_path)
    due = h1_due_now()
    candles = FakeCandles(due)
    script_llm(monkeypatch, [
        json.dumps({"tool": "get_closed_candles", "arguments": {
            "symbol": SYMBOL, "timeframe": "4h", "limit": 120,
        }}),
        json.dumps(make_entry_intent(due)),
    ])
    trader_q = world.bus.subscribe({EventType.TRADER_INTENT_CREATED})
    gm_q = world.bus.subscribe(
        {
            EventType.GM_ENTRY_APPROVED,
            EventType.GM_ENTRY_REJECTED,
            EventType.RECONCILIATION_REQUIRED,
        }
    )
    watch_q = world.bus.subscribe(
        {
            EventType.POSITION_OPENED,
            EventType.POSITION_CHANGED,
            EventType.ORDER_CHANGED,
            EventType.FILL,
        }
    )

    h1 = run(
        world.clock(candles).publish_closed_bar(
            SYMBOL, "1h", due, EventType.H1_BAR_CLOSED
        )
    )
    cid = h1.correlation_id
    run(world.trader(candles, shadow_mode=False).handle(h1))
    (trader_event,) = world.drain(trader_q)

    gm_event = run(world.gm_consumer.handle(trader_event))
    assert gm_event.type == EventType.GM_ENTRY_APPROVED
    binding = gm_event.payload["binding"]
    assert binding["correlation_id"] == cid
    assert binding["main_position_id"] is None
    assert binding["status"] == "submitted"
    assert read_binding(world, cid)["main_position_id"] is None
    [(kind, open_kwargs)] = world.port.calls
    assert kind == "open"
    assert open_kwargs["side"] == "LONG" and open_kwargs["quantity"] == D("1.00")

    # Venue has not shown the position yet: reconciliation stays submitted.
    lagging = run(world.gm.reconcile_main(cid))
    assert lagging["status"] == "submitted" and lagging["main_position_id"] is None

    world.port.commit_main()
    reconciled = run(world.gm.reconcile_main(cid))
    assert reconciled["status"] == "reconciled"
    main_id = reconciled["main_position_id"]
    assert isinstance(main_id, str) and main_id.startswith("pos-")

    emitted = run(world.watcher().poll())
    opened = [e for e in emitted if e.type == EventType.POSITION_OPENED]
    assert len(opened) == 1 and opened[0].correlation_id == cid
    bus_opened = [
        e for e in world.drain(watch_q) if e.type == EventType.POSITION_OPENED
    ]
    assert len(bus_opened) == 1 and bus_opened[0].correlation_id == cid
    assert world.store.read_events(), "bus must durably persist every event"


def test_pm_timer_hold_makes_zero_writes(tmp_path, monkeypatch):
    """Scenario 3 (PM HOLD): MAIN open -> PM_TIMER -> production context builder.

    The PM runs the REAL run_role tool loop (LLM client mocked) and decides
    HOLD: the decision is saved + published and the port sees zero new writes.
    """
    world = E2EWorld(tmp_path)
    binding = open_main_position(world, "c-hold", h1_due_now())
    main_id = binding["main_position_id"]
    assert main_id

    moment = 1_800_000_000_000
    world.pm_now = moment
    fake_llm = script_llm(monkeypatch, [json.dumps(hold_decision(moment, main_id))])
    mgmt_q = world.bus.subscribe({EventType.MANAGEMENT_INTENT_CREATED})

    (decision,) = run(
        world.pm.handle_event(
            {"type": "PM_TIMER", "symbol": SYMBOL, "event_id": "timer-1"}
        )
    )
    assert decision.action == "HOLD"
    assert decision.position_ids == [main_id]
    assert fake_llm.started and fake_llm.stopped and len(fake_llm.prompts) == 1

    assert world.port.write_kinds() == ["open"]
    (event,) = world.drain(mgmt_q)
    assert event.payload["action"] == "HOLD" and event.correlation_id == "c-hold"
    assert world.saved_pm == [("c-hold", decision)]


def test_pm_reduce_half_computes_quantized_close_qty(tmp_path, monkeypatch):
    """Scenario 4 (REDUCE): MAIN qty 1 -> PM REDUCE 0.5 through GMConsumer.

    GM quantizes the close to 0.50 (step 0.01), writes the management record,
    and the venue MAIN leg drops to 0.5.
    """
    world = E2EWorld(tmp_path)
    binding = open_main_position(world, "c-reduce", h1_due_now())
    main_id = binding["main_position_id"]

    moment = 1_800_000_000_000
    world.pm_now = moment
    script_llm(monkeypatch, [json.dumps(reduce_decision(moment, main_id, "0.5"))])
    mgmt_q = world.bus.subscribe({EventType.MANAGEMENT_INTENT_CREATED})
    gm_q = world.bus.subscribe(
        {EventType.GM_MANAGEMENT_APPROVED, EventType.GM_MANAGEMENT_REJECTED}
    )

    decision = run(
        world.pm.handle_event(
            {
                "type": "POSITION_CHANGED",
                "correlation_id": "c-reduce",
                "event_id": "e-1",
            }
        )
    )
    assert decision.action == "REDUCE" and decision.reduce_fraction == "0.5"
    (mgmt_event,) = world.drain(mgmt_q)

    gm_event = run(world.gm_consumer.handle(mgmt_event))
    assert gm_event.type == EventType.GM_MANAGEMENT_APPROVED
    assert gm_event.payload["result"]["quantity"] == "0.50"

    reduces = [call for call in world.port.calls if call[0] == "reduce"]
    assert len(reduces) == 1
    assert reduces[0][1]["quantity"] == Decimal("0.50")

    records = list((world.root / "trades" / "c-reduce" / "management").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "submitted" and record["quantity"] == "0.50"

    main_row = next(
        row for row in world.venue.positions if row["position_id"] == main_id
    )
    assert D(main_row["net_amount_base"]) == D("0.5")


def test_open_orders_timeout_blocks_management_with_zero_writes(tmp_path):
    """Scenario 9 (OPEN ORDERS FAILURE): unreadable order book is UNKNOWN.

    Entry and management both fail closed with ZERO port writes; through
    GMConsumer the explicit rejection surfaces as GM_MANAGEMENT_REJECTED.
    """
    world = E2EWorld(tmp_path)
    world.venue.fail_orders = True

    with pytest.raises(GMRejected, match="open orders"):
        run(
            world.gm.execute_entry(
                make_entry_intent(h1_due_now()), correlation_id="c-x"
            )
        )
    assert world.port.calls == []
    assert not (world.root / "trades" / "c-x").exists()

    world.venue.fail_orders = False
    binding = open_main_position(world, "c-blocked", h1_due_now())
    writes_after_entry = len(world.port.calls)
    assert writes_after_entry == 1

    world.venue.fail_orders = True
    with pytest.raises(GMRejected, match="open orders"):
        run(
            world.gm.execute_management(
                correlation_id="c-blocked",
                decision_id="d-1",
                action="REDUCE",
                reduce_fraction=D("0.5"),
            )
        )
    assert len(world.port.calls) == writes_after_entry

    rejected = run(
        world.gm_consumer.handle(
            BrooksEvent(
                type=EventType.MANAGEMENT_INTENT_CREATED,
                symbol=SYMBOL,
                correlation_id="c-blocked",
                payload={
                    "action": "REDUCE",
                    "decision_id": "d-2",
                    "reduce_fraction": "0.5",
                },
            )
        )
    )
    assert rejected.type == EventType.GM_MANAGEMENT_REJECTED
    assert rejected.payload["action"] == "REDUCE"
    assert len(world.port.calls) == writes_after_entry


def test_supervisor_default_list_active_finds_live_binding(tmp_path):
    """The production global-wake lookup finds the GM binding root."""
    from condor.brooks.supervisor import BrooksSupervisor

    world = E2EWorld(tmp_path)
    open_main_position(world, "c-list", h1_due_now())
    assert (world.root / "trades" / "c-list" / "binding.json").exists()

    supervisor = BrooksSupervisor.__new__(BrooksSupervisor)
    supervisor.store = world.store
    supervisor.strategy_home = tmp_path
    found = run(BrooksSupervisor._default_pm_list_active(supervisor, SYMBOL))
    assert found == ["c-list"]
