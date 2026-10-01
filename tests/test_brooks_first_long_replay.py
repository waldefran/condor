"""First recorded MAIN reaches the old stop and locks through the real GM."""

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from condor.brooks import gm as gm_module
from condor.brooks.contracts import ManagementDecisionV2
from condor.brooks.events import BrooksEvent, EventType
from scripts.brooks_walkforward import WalkForward, utc_ms
from tests.brooks_e2e_harness import hold_decision


CID = "ETH-USDT-1h-1789883999999"
SELECTED_CID = "ETH-USDT-1h-1790002799999"
REPO = Path(__file__).resolve().parents[1]
RECORDED_SOURCE = REPO / "docs/brooks_walkforward_deepseek_v4_1_flash_corrected_2026-09-20_2026-09-29"


def _args(tmp_path):
    return SimpleNamespace(output=tmp_path / "output", state=tmp_path / "state",
        dataset=Path("/tmp/brooks-walkforward-10d-data"), symbol="ETH-USDT",
        recorded_run=RECORDED_SOURCE,
        start="2026-09-20T06:00:00Z", end="2026-09-21T06:00:00Z",
        agent_key="custom@opencode-go:deepseek-v4.1-flash", initial_equity="10000",
        fee_rate="0.0004", slippage_bps="1", git_head="test", only_trade=CID,
        long_lock_policy=True)


def _trader_source_subset(tmp_path):
    source = tmp_path / "source"
    (source / "role_runs").mkdir(parents=True)
    (source / "frozen_packets").mkdir()
    (source / "wire_requests").mkdir()
    rows = [json.loads(line) for line in (RECORDED_SOURCE / "cycles.jsonl").read_text().splitlines()]
    selected_index = next(i for i, row in enumerate(rows) if row["correlation_id"] == SELECTED_CID)
    selected, next_accepted, failed = (dict(row) for row in rows[selected_index:selected_index + 3])
    failed.update(status="failed", intent=None)
    failed["cycle"] = {**failed["cycle"], "status": "failed", "intent": None}
    subset = [selected, next_accepted, failed]
    for row in subset:
        packet = row["frozen_packet_file"]
        target = source / packet
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RECORDED_SOURCE / packet, target)
    (source / "cycles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in subset))
    (source / "run_manifest.json").write_text(json.dumps({"model": "immutable-fixture"}) + "\n")
    return source


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/tmp/brooks-walkforward-10d-data/ETH_USDT_1m.jsonl").exists(),
    reason="historical first-operation integration requires the downloaded immutable M1 dataset")
async def test_first_long_old_stop_locks_and_negative_exit_is_blocked(tmp_path, monkeypatch):
    runner = WalkForward(_args(tmp_path))
    monkeypatch.setattr(gm_module, "time", SimpleNamespace(time=lambda: runner.now / 1000))
    assert sum(x["kind"] == "recorded_trader" for x in runner.agenda) == 1
    # This is the historical minute that previously closed MAIN by STOP_LOSS.
    await runner.advance_to(utc_ms("2026-09-20T08:52:00Z") - 1)
    assert len(runner.venue.trades) == 1
    trade = runner.venue.trades[0]
    assert trade.correlation_id == CID and trade.closed_at_ms is None
    state = runner.venue.long_policy_context(CID)
    assert state["main_quantity"] == state["hedge_quantity"] == "0.604"
    assert float(state["projected_exit_net"]) < 0
    binding = runner.store.read_trade_document(CID, "binding.json")
    assert binding["status"] == "reconciled"
    assert binding["main_position_id"] and binding["hedge_position_id"]
    assert not any(fill.get("reason") == "STOP_LOSS" for fill in runner.venue.fills)
    context = await runner.load_pm_context(CID)
    assert context["management_policy"]["applicable_risk_behavior"]["long_exit_policy"] == "lock_and_wait_nonnegative_net"
    assert context["management_policy"]["applicable_risk_behavior"]["operation_correlation_id"] == CID
    assert context["hedge_state"]["hedge_ratio"] == "1"
    event = BrooksEvent(EventType.MANAGEMENT_INTENT_CREATED, "ETH-USDT",
        {"action": "CLOSE"}, correlation_id=CID)
    assert "negative" in runner._long_management_rejection(event)
    before_fills = len(runner.venue.fills)
    await runner.events.publish(event)
    await runner.drain()
    outcome = json.loads((runner.root / "management_outcomes.jsonl").read_text().splitlines()[-1])
    assert outcome["gm_result"]["type"] == "GM_MANAGEMENT_REJECTED"
    assert len(runner.venue.fills) == before_fills
    assert runner.store.read_trade_document(CID, "binding.json")["status"] == "reconciled"
    proof = json.loads((runner.root / "long_lock_events.jsonl").read_text().splitlines()[0])
    assert proof["gm_result"]["assessment"] == "confirmed"
    assert proof["simulation_time_ms"] == 1789894319999
    runner.events.close()
    runner.store.flush()


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/tmp/brooks-walkforward-10d-data/ETH_USDT_1m.jsonl").exists(),
    reason="historical integration requires the downloaded immutable M1 dataset")
async def test_selected_long_policy_scope_and_duration_wake(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.only_trade = "ETH-USDT-1h-1790002799999"
    args.start, args.end = "2026-09-21T15:00:00Z", "2026-09-24T15:00:00Z"
    args.max_unhedged_loss_r = "5"
    runner = WalkForward(args)
    monkeypatch.setattr(gm_module, "time", SimpleNamespace(time=lambda: runner.now / 1000))
    await runner.advance_to(utc_ms("2026-09-21T16:00:00Z") - 1)
    assert len(runner.venue.trades) == 1
    trade = runner.venue.trades[0]
    assert trade.correlation_id == args.only_trade and trade.opened_at_ms == 1790005679999
    context = await runner.load_pm_context(args.only_trade)
    policy = context["management_policy"]["applicable_risk_behavior"]
    assert context["correlation_id"] == policy["operation_correlation_id"] == args.only_trade
    assert policy["max_unhedged_loss_r"] == "5"
    assert await runner.load_pm_context(CID) is None
    initial_deadline = runner.venue.long_policy_context(args.only_trade)["duration_expires_at_ms"]
    assert initial_deadline == trade.opened_at_ms + 86400 * 1000
    await runner.advance_to(initial_deadline)
    assert trade.closed_at_ms is None
    assert not any(f.get("reason") in ("STOP_LOSS", "TIME_LIMIT") for f in runner.venue.fills)
    duration_records = [json.loads(row) for row in
        (runner.root / "long_duration_events.jsonl").read_text().splitlines()]
    proof = next(row for row in duration_records
        if row.get("correlation_id") == args.only_trade and row.get("observed_at_ms") is not None)
    queued = next(item for item in runner.agenda
        if item["kind"] == "pm"
        and item["event"].get("payload", {}).get("duration_limit_reached") == proof)
    timer = BrooksEvent.from_dict(queued["event"])
    assert timer.type == EventType.PM_TIMER
    assert timer.correlation_id == args.only_trade
    assert timer.created_at_ms == proof["observed_at_ms"] == runner.now
    assert timer.payload["duration_limit_reached"] == proof

    model_calls = []

    async def fake_readonly_model(role, **kwargs):
        assert role == "POSITION_MANAGER"
        assert kwargs["output_model"] is ManagementDecisionV2
        prompt = kwargs["prompt"]
        positions = prompt.get("positions") or [prompt.get("position")]
        position = next(row for row in positions if row is not None)
        decision = ManagementDecisionV2.model_validate(
            hold_decision(prompt["decision_time_ms"], position["position_id"])
        )
        model_calls.append(decision)
        return decision

    runner.pm.runner = fake_readonly_model
    before_fills = list(runner.venue.fills)
    before_executors = list(runner.venue.executor_rows)
    before_submissions = list(runner.venue.entry_submissions)
    result, _ = await runner.scoped(
        "pm-duration-test", lambda: runner.pm.handle_event(timer.to_dict())
    )
    assert isinstance(result, ManagementDecisionV2) and result.action == "HOLD"
    assert len(model_calls) == 1
    await runner.drain()
    outcome = json.loads((runner.root / "management_outcomes.jsonl").read_text().splitlines()[-1])
    assert outcome["decision"]["action"] == "HOLD"
    assert outcome["gm_result"]["type"] == "GM_MANAGEMENT_APPROVED"
    state = runner.venue.long_policy_context(args.only_trade)
    assert state["duration_expires_at_ms"] > initial_deadline
    assert trade.closed_at_ms is None
    assert runner.venue.trades == [trade]
    assert runner.venue.fills == before_fills
    assert runner.venue.executor_rows == before_executors
    assert runner.venue.entry_submissions == before_submissions
    updated_duration_records = [json.loads(row) for row in
        (runner.root / "long_duration_events.jsonl").read_text().splitlines()]
    extensions = [row for row in updated_duration_records if row.get("action") == "HOLD_EXTENSION"]
    assert extensions
    assert extensions[-1]["expires_at_ms"] == state["duration_expires_at_ms"]
    assert extensions[-1]["expires_at_ms"] == (
        extensions[-1]["simulation_time_ms"] + runner.pm_interval_sec * 1000
    )
    assert runner.venue.long_policy_context(args.only_trade)["initial_R_usdt"] == str(trade.initial_risk_usd)
    runner.events.close()
    runner.store.flush()


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/tmp/brooks-walkforward-10d-data/ETH_USDT_1m.jsonl").exists(),
    reason="historical replay requires the downloaded immutable M1 dataset")
async def test_selected_trade_replay_refreshes_only_later_trader_context(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.only_trade = SELECTED_CID
    args.recorded_run = _trader_source_subset(tmp_path)
    args.start, args.end = "2026-09-21T15:00:00Z", "2026-09-21T18:00:00Z"
    runner = WalkForward(args)
    monkeypatch.setattr(gm_module, "time", SimpleNamespace(time=lambda: runner.now / 1000))
    next_due = 1790006549351
    try:
        trader_outputs = [item for item in runner.agenda
            if item["kind"] in ("recorded_trader", "recorded_trader_context")]
        assert [item["kind"] for item in trader_outputs] == [
            "recorded_trader", "recorded_trader_context"]
        assert runner.manifest["source_trader_cycles"] == 1
        assert trader_outputs[1]["due"] == next_due

        # The selected MAIN is active before the next source analysis completes.
        await runner.advance_to(next_due - 1)
        assert len(runner.venue.trades) == 1
        trade = runner.venue.trades[0]
        assert trade.correlation_id == SELECTED_CID and trade.opened_at_ms == 1790005679999
        latest_path = runner.store.root / "trader" / "latest.json"
        latest = json.loads(latest_path.read_text())
        assert latest["decision_time_ms"] == 1790002799999
        assert not (runner.root / "trader_context_updates.jsonl").exists()

        await runner.advance_to(next_due)
        latest = json.loads(latest_path.read_text())
        assert latest["decision_time_ms"] == 1790006399999
        assert latest["decision"] == "NO_TRADE"
        updates = [json.loads(line) for line in
            (runner.root / "trader_context_updates.jsonl").read_text().splitlines()]
        assert len(updates) == 1
        assert updates[0]["decision"] == "NO_TRADE"
        assert updates[0]["available_at_ms"] == next_due
        assert updates[0]["source_line"] == 2
        assert updates[0]["intent"]["decision_time_ms"] == latest["decision_time_ms"]

        assert len(runner.venue.trades) == 1
        assert runner.venue.trades[0].correlation_id == SELECTED_CID
        assert len(runner.venue.entry_submissions) == 1
        trader_events = [event for event in runner.store.read_events()
            if event.type == EventType.TRADER_INTENT_CREATED]
        assert len(trader_events) == 1
        assert trader_events[0].correlation_id == SELECTED_CID
        cycles = [json.loads(line) for line in (runner.root / "cycles.jsonl").read_text().splitlines()]
        assert len(cycles) == 1
        assert cycles[0]["correlation_id"] == SELECTED_CID
    finally:
        runner.events.close()
        runner.store.flush()


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/tmp/brooks-walkforward-10d-data/ETH_USDT_1m.jsonl").exists(),
    reason="historical first-operation integration requires the downloaded immutable M1 dataset")
async def test_first_long_five_r_keeps_original_r_and_reaches_positive_tp(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.max_unhedged_loss_r = "5"
    runner = WalkForward(args)
    monkeypatch.setattr(gm_module, "time", SimpleNamespace(time=lambda: runner.now / 1000))
    await runner.advance_to(utc_ms("2026-09-20T08:52:00Z") - 1)
    state = runner.venue.long_policy_context(CID)
    assert state["main_quantity"] == "0.604" and state["hedge_quantity"] == "0"
    assert state["initial_R_usdt"] == "4.995080000000000000000000000"
    assert float(state["allowed_loss_usdt"]) == pytest.approx(24.9754)
    await runner.advance_to(utc_ms("2026-09-20T12:00:00Z") - 1)
    assert runner.venue.trades[0].closed_at_ms is None
    assert not (runner.root / "long_lock_events.jsonl").exists()
    assert runner.venue.long_policy_context(CID)["initial_R_usdt"] == state["initial_R_usdt"]
    assert runner.venue.long_policy_context(CID)["hedge_quantity"] == "0"
    await runner.advance_to(utc_ms("2026-09-20T16:00:00Z") - 1)
    assert runner.venue.trades[0].closed_at_ms == 1789916519999
    assert runner.venue.trades[0].realized_gross_pnl - runner.venue.trades[0].fees > 0
    runner.events.close()
    runner.store.flush()
