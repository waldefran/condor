"""First recorded MAIN reaches the old stop and locks through the real GM."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from condor.brooks import gm as gm_module
from condor.brooks.events import BrooksEvent, EventType
from scripts.brooks_walkforward import WalkForward, utc_ms


CID = "ETH-USDT-1h-1789883999999"
REPO = Path(__file__).resolve().parents[1]


def _args(tmp_path):
    return SimpleNamespace(output=tmp_path / "output", state=tmp_path / "state",
        dataset=Path("/tmp/brooks-walkforward-10d-data"), symbol="ETH-USDT",
        recorded_run=REPO / "docs/brooks_walkforward_deepseek_v4_1_flash_corrected_2026-09-20_2026-09-29",
        start="2026-09-20T06:00:00Z", end="2026-09-21T06:00:00Z",
        agent_key="custom@opencode-go:deepseek-v4.1-flash", initial_equity="10000",
        fee_rate="0.0004", slippage_bps="1", git_head="test", only_trade=CID,
        long_lock_policy=True)


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
