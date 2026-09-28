"""Wave 3 Brooks E2E, part 2: hedge lifecycle (0.30 -> 0.50 -> 0.20 -> 0) + restart.

Same ground rules as test_brooks_e2e.py: real production classes throughout,
only the Hummingbot client + ExecutionPort faked, only the LLM client mocked.

Two production gaps found while traversing are fixed alongside this suite
and pinned here as regression tests:

- First-HEDGE confirmation: the GM resolves the new hedge leg through hedge
  executor lineage (adapters ``reconcile_hedge``) instead of sticking the
  binding at ``reconciliation_required``.
- Consumer-routed first HEDGE: BrooksGM builds the expected HedgeState from
  a fresh reader read when no ``expected_state``/``hedge_state.json`` exists;
  GMConsumer keeps passing the full decision untouched.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import tests.brooks_e2e_harness as harness
from condor.brooks.contracts import ManagementDecisionV2
from condor.brooks.events import BrooksEvent, EventType
from condor.brooks.hedge import PositionLeg, build_hedge_state
from tests.brooks_e2e_harness import (
    SYMBOL,
    D,
    E2EWorld,
    FakeCandles,
    expected_from_reader,
    h1_due_now,
    hedge_decision,
    open_main_position,
    read_binding,
    run,
    script_llm,
    seed_reconciled_hedge,
)


def no_trade_intent(due_ms: int) -> dict:
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "NO_TRADE",
        "symbol": SYMBOL,
        "decision_time_ms": due_ms,
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


def venue_hedge_ratio(world: E2EWorld, main_id: str) -> str:
    """Fresh HedgeState straight from the current venue legs (pure production fn)."""
    legs = []
    for row in world.venue.positions:
        if row["position_id"] == main_id:
            legs.append(
                PositionLeg(
                    main_id,
                    SYMBOL,
                    "LONG",
                    str(row["net_amount_base"]),
                    str(world.venue.mark_price),
                    "MAIN",
                )
            )
        elif row["position_id"] == world.port.hedge_position_id:
            legs.append(
                PositionLeg(
                    world.port.hedge_position_id,
                    SYMBOL,
                    "SHORT",
                    str(row["net_amount_base"]),
                    str(world.venue.mark_price),
                    "HEDGE",
                )
            )
    main_legs = [leg for leg in legs if leg.ownership_role == "MAIN"]
    hedge_legs = [leg for leg in legs if leg.ownership_role == "HEDGE"]
    state = build_hedge_state(
        legs,
        main_position_id=main_legs[0].position_id if main_legs else None,
        hedge_position_id=hedge_legs[0].position_id if hedge_legs else None,
        as_of_ms=time.time_ns() // 1_000_000,
    )
    assert state.structure_status == "ok", state.structure_status
    return state.hedge_ratio


def pm_hedge_round(world: E2EWorld, cid: str, moment: int, script: dict):
    """Run the REAL PM loop (run_role, mocked LLM): returns (decision, bus event)."""
    world.pm_now = moment
    mgmt_q = world.bus.subscribe({EventType.MANAGEMENT_INTENT_CREATED})
    decision = run(
        world.pm.handle_event(
            {"type": "POSITION_CHANGED", "correlation_id": cid, "event_id": "e-pm"}
        )
    )
    (mgmt_event,) = world.drain(mgmt_q)
    assert mgmt_event.payload == ManagementDecisionV2.model_validate(script).model_dump(
        mode="json"
    )
    return decision, mgmt_event


def gm_hedge(
    world: E2EWorld,
    cid: str,
    decision_id: str,
    action: str,
    target: str,
    main_id: str,
    hedge_id: str | None,
) -> dict:
    return run(
        world.gm.execute_management(
            correlation_id=cid,
            decision_id=decision_id,
            action=action,
            target_hedge_ratio=target,
            expected_state=expected_from_reader(world),
            plan_main_position_id=main_id,
            plan_hedge_position_id=hedge_id,
        )
    )


def test_hedge_open_sends_sell_delta_and_reaches_ratio(tmp_path, monkeypatch):
    """Scenario 5 (HEDGE): MAIN LONG 1 -> PM HEDGE 0.30 -> SELL OPEN via port."""
    world = E2EWorld(tmp_path)
    main_id = open_main_position(world, "c-hedge", h1_due_now())["main_position_id"]

    moment = 1_800_000_000_000
    script = hedge_decision(moment, main_id, "HEDGE", "0.30", None)
    script_llm(monkeypatch, [json.dumps(script)])
    decision, _ = pm_hedge_round(world, "c-hedge", moment, script)
    assert decision.action == "HEDGE"

    result = gm_hedge(world, "c-hedge", "d-h1", "HEDGE", "0.30", main_id, None)
    assert result["status"] == "submitted"
    assert result["executor_id"].startswith("exec-")

    # The write itself is exactly right: SELL OPEN of the 0.30 delta.
    hedge_calls = [call for call in world.port.calls if call[0] == "hedge"]
    assert len(hedge_calls) == 1
    assert hedge_calls[0][1]["side"] == "SELL"
    assert hedge_calls[0][1]["position_action"] == "OPEN"
    assert hedge_calls[0][1]["quantity"] == Decimal("0.30")
    hedge_row = next(
        row
        for row in world.venue.positions
        if row["position_id"] == world.port.hedge_position_id
    )
    assert D(hedge_row["net_amount_base"]) == D("0.3")
    assert venue_hedge_ratio(world, main_id) == "0.3"

    # Confirmation resolved the new leg through hedge executor lineage and
    # bound it: no stuck reconciliation_required, later hedge writes unblocked.
    bound = read_binding(world, "c-hedge")
    assert bound["hedge_position_id"] == world.port.hedge_position_id
    assert bound["status"] in ("submitted", "reconciled")


def test_hedge_consumer_first_open_approved_with_bound_leg(tmp_path, monkeypatch):
    """Consumer-routed first HEDGE: GM builds expected state, binds the leg."""
    world = E2EWorld(tmp_path)
    main_id = open_main_position(world, "c-hb", h1_due_now())["main_position_id"]

    moment = 1_800_000_000_000
    script = hedge_decision(moment, main_id, "HEDGE", "0.30", None)
    script_llm(monkeypatch, [json.dumps(script)])
    _, mgmt_event = pm_hedge_round(world, "c-hb", moment, script)
    assert isinstance(mgmt_event, BrooksEvent)

    result = run(world.gm_consumer.handle(mgmt_event))
    assert result.type == EventType.GM_MANAGEMENT_APPROVED
    assert result.payload["result"]["quantity"] == "0.30"
    hedge_calls = [call for call in world.port.calls if call[0] == "hedge"]
    assert len(hedge_calls) == 1
    assert hedge_calls[0][1]["side"] == "SELL"
    assert hedge_calls[0][1]["position_action"] == "OPEN"
    assert hedge_calls[0][1]["quantity"] == Decimal("0.30")
    bound = read_binding(world, "c-hb")
    assert bound["hedge_position_id"] == world.port.hedge_position_id


def test_increase_hedge_opens_only_the_delta(tmp_path, monkeypatch):
    """Scenario 6 (INCREASE_HEDGE): 0.30 -> 0.50 opens only the 0.20 delta."""
    world = E2EWorld(tmp_path)
    main_id = open_main_position(world, "c-inc", h1_due_now())["main_position_id"]
    seed_reconciled_hedge(world, "c-inc", "0.3")

    moment = 1_800_000_000_000
    script = hedge_decision(moment, main_id, "INCREASE_HEDGE", "0.50", "hpos-1")
    script_llm(monkeypatch, [json.dumps(script)])
    decision, _ = pm_hedge_round(world, "c-inc", moment, script)
    assert decision.action == "INCREASE_HEDGE"

    result = gm_hedge(
        world, "c-inc", "d-inc", "INCREASE_HEDGE", "0.50", main_id, "hpos-1"
    )
    assert result["status"] == "submitted"
    assert result["quantity"] == "0.20"
    hedge_calls = [call for call in world.port.calls if call[0] == "hedge"]
    assert hedge_calls[-1][1] == {
        "symbol": SYMBOL,
        "side": "SELL",
        "quantity": Decimal("0.20"),
        "position_action": "OPEN",
        "leverage": 2,
    }
    assert venue_hedge_ratio(world, main_id) == "0.5"
    assert D(read_binding(world, "c-inc")["hedge_size"]) == D("0.5")


def test_reduce_hedge_closes_only_the_delta(tmp_path, monkeypatch):
    """Scenario 7 (REDUCE_HEDGE): 0.50 -> 0.20 closes only the 0.30 delta."""
    world = E2EWorld(tmp_path)
    main_id = open_main_position(world, "c-dec", h1_due_now())["main_position_id"]
    seed_reconciled_hedge(world, "c-dec", "0.5")

    moment = 1_800_000_000_000
    script = hedge_decision(moment, main_id, "REDUCE_HEDGE", "0.20", "hpos-1")
    script_llm(monkeypatch, [json.dumps(script)])
    decision, _ = pm_hedge_round(world, "c-dec", moment, script)
    assert decision.action == "REDUCE_HEDGE"

    result = gm_hedge(
        world, "c-dec", "d-dec", "REDUCE_HEDGE", "0.20", main_id, "hpos-1"
    )
    assert result["status"] == "submitted"
    assert result["quantity"] == "0.30"
    hedge_calls = [call for call in world.port.calls if call[0] == "hedge"]
    assert hedge_calls[-1][1]["side"] == "BUY"
    assert hedge_calls[-1][1]["position_action"] == "CLOSE"
    assert hedge_calls[-1][1]["quantity"] == Decimal("0.30")
    assert venue_hedge_ratio(world, main_id) == "0.2"


def test_remove_hedge_closes_remaining_leg(tmp_path, monkeypatch):
    """Scenario 8 (REMOVE_HEDGE): 0.20 -> 0 closes the remaining hedge."""
    world = E2EWorld(tmp_path)
    main_id = open_main_position(world, "c-rem", h1_due_now())["main_position_id"]
    seed_reconciled_hedge(world, "c-rem", "0.2")

    moment = 1_800_000_000_000
    script = hedge_decision(moment, main_id, "REMOVE_HEDGE", "0", "hpos-1")
    script_llm(monkeypatch, [json.dumps(script)])
    decision, _ = pm_hedge_round(world, "c-rem", moment, script)
    assert decision.action == "REMOVE_HEDGE"

    result = gm_hedge(world, "c-rem", "d-rem", "REMOVE_HEDGE", "0", main_id, "hpos-1")
    assert result["status"] == "submitted"
    assert result["quantity"] == "0.20"
    hedge_calls = [call for call in world.port.calls if call[0] == "hedge"]
    assert hedge_calls[-1][1]["position_action"] == "CLOSE"
    assert hedge_calls[-1][1]["quantity"] == Decimal("0.20")
    assert [
        row
        for row in world.venue.positions
        if row["position_id"] == world.port.hedge_position_id
    ] == []
    bound = read_binding(world, "c-rem")
    assert bound["hedge_position_id"] is None and bound["hedge_size"] == "0"


def test_restart_reconciles_and_manages_without_new_trade(tmp_path, monkeypatch):
    """Scenario 10 (RESTART): process B resumes MAIN+HEDGE from durable state.

    A stops with MAIN + 0.5 hedge bound and persisted; B (fresh objects, same
    root, same venue rows) re-queries the venue, reconciles MAIN, sees the
    watcher recognize POSITION_OPENED + HEDGE_OPENED, wakes the PM on that
    event, and REDUCE_HEDGE 0.5 -> 0.2 confirms through GMConsumer
    (expected_state loads from the persisted hedge_state.json). No new MAIN
    trade is opened.
    """
    # ---- process A: trade, persist, stop ----
    proc_a = E2EWorld(tmp_path)
    main_id = open_main_position(proc_a, "c-restart", h1_due_now())["main_position_id"]
    seed_reconciled_hedge(proc_a, "c-restart", "0.5")
    proc_a.store.flush()
    proc_a.bus.close()
    trades_before = sorted(path.name for path in (tmp_path / "trades").iterdir())
    assert trades_before == ["c-restart"]
    del proc_a

    # ---- process B: fresh objects, same durable root, same venue rows ----
    proc_b = E2EWorld(tmp_path)
    proc_b.venue.positions = [
        {
            "position_id": main_id,
            "trading_pair": SYMBOL,
            "position_side": "LONG",
            "net_amount_base": "1.00",
            "current_price": "120",
        },
        {
            "position_id": "hpos-1",
            "trading_pair": SYMBOL,
            "position_side": "SHORT",
            "net_amount_base": "0.5",
            "current_price": "120",
        },
    ]
    proc_b.venue.executor_rows = [
        {"executor_id": "exec-1", "status": "RUNNING", "trading_pair": SYMBOL},
        {"executor_id": "exec-seed-hedge", "status": "RUNNING", "trading_pair": SYMBOL},
    ]
    proc_b.venue.holds = [
        {
            "account_name": harness.ACCOUNT,
            "connector_name": harness.CONNECTOR,
            "trading_pair": SYMBOL,
            "position_side": "LONG",
            "net_amount_base": 1.0,
            "controller_id": harness.CONTROLLER,
            "executor_ids": ["exec-1"],
        },
    ]

    # B re-queries the venue: MAIN + HEDGE legs resolve from persisted binding.
    snapshot = run(
        proc_b.reader.read(
            account_name=harness.ACCOUNT,
            connector_name=harness.CONNECTOR,
            symbol=SYMBOL,
        )
    )
    assert snapshot.structure_status == "single_main"
    assert snapshot.main_position_id == main_id
    assert snapshot.hedge_position_id == "hpos-1"
    assert D(snapshot.main_quantity) == D("1") and D(snapshot.hedge_quantity) == D(
        "0.5"
    )

    again = run(proc_b.gm.reconcile_main("c-restart"))
    assert again["main_position_id"] == main_id

    # The watcher recognizes the resumed state on its first poll.
    watch_q = proc_b.bus.subscribe(
        {EventType.POSITION_OPENED, EventType.HEDGE_OPENED, EventType.ORDER_CHANGED}
    )
    emitted = run(proc_b.watcher().poll())
    kinds = {event.type for event in emitted}
    assert EventType.POSITION_OPENED in kinds and EventType.HEDGE_OPENED in kinds

    # The PM wakes on the watcher event; REDUCE_HEDGE 0.5 -> 0.2 confirms.
    moment = 1_800_000_000_000
    proc_b.pm_now = moment
    script_llm(
        monkeypatch,
        [json.dumps(hedge_decision(moment, main_id, "REDUCE_HEDGE", "0.20", "hpos-1"))],
    )
    mgmt_q = proc_b.bus.subscribe({EventType.MANAGEMENT_INTENT_CREATED})
    gm_q = proc_b.bus.subscribe({EventType.GM_MANAGEMENT_APPROVED})
    opened = next(
        e for e in proc_b.drain(watch_q) if e.type == EventType.POSITION_OPENED
    )
    decision = run(proc_b.pm.handle_event(opened))
    assert decision.action == "REDUCE_HEDGE"
    (mgmt_event,) = proc_b.drain(mgmt_q)
    gm_event = run(proc_b.gm_consumer.handle(mgmt_event))
    assert gm_event.type == EventType.GM_MANAGEMENT_APPROVED
    assert gm_event.payload["result"]["quantity"] == "0.30"
    assert D(
        next(row for row in proc_b.venue.positions if row["position_id"] == "hpos-1")[
            "net_amount_base"
        ]
    ) == D("0.2")

    # No accidental new trade during restart: same single binding, no MAIN open.
    assert (
        sorted(path.name for path in (tmp_path / "trades").iterdir()) == trades_before
    )
    assert "open" not in proc_b.port.write_kinds()

    # A fresh H1 bar that the (mocked) Trader answers NO_TRADE to stays silent.
    due2 = h1_due_now()
    candles2 = FakeCandles(due2)
    script_llm(monkeypatch, [json.dumps(no_trade_intent(due2))])
    trader_q = proc_b.bus.subscribe({EventType.TRADER_INTENT_CREATED})
    h1 = run(
        proc_b.clock(candles2).publish_closed_bar(
            SYMBOL, "1h", due2, EventType.H1_BAR_CLOSED
        )
    )
    run(proc_b.trader(candles2, shadow_mode=False).handle(h1))
    (no_trade_event,) = proc_b.drain(trader_q)
    assert run(proc_b.gm_consumer.handle(no_trade_event)) is None
    assert "open" not in proc_b.port.write_kinds()
