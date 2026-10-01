"""Recorded Trader/analyst outputs are available only at their source completion."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.brooks_walkforward import WalkForward, utc_ms
from scripts.brooks_walkforward_report import _role_runs_for_record


SOURCE = Path(__file__).resolve().parents[1] / "docs/brooks_walkforward_deepseek_v4_1_flash_corrected_2026-09-20_2026-09-29"


def _scheduler(source: Path) -> WalkForward:
    runner = object.__new__(WalkForward)
    runner.recorded_run = source
    runner.start = utc_ms("2026-09-20T00:00:00Z")
    runner.end = utc_ms("2026-09-25T11:00:00Z")
    runner.symbol = "ETH-USDT"
    runner.agenda = []
    runner.sequence = 0
    return runner


def test_recorded_agenda_preserves_original_completion_times():
    runner = _scheduler(SOURCE)
    runner._schedule_recorded_outputs()
    traders = [item for item in runner.agenda if item["kind"] == "recorded_trader"]
    contexts = [item for item in runner.agenda if item["kind"] == "recorded_context"]
    source_cycles = [json.loads(line) for line in (SOURCE / "cycles.jsonl").read_text().splitlines()]
    assert len(traders) == len(source_cycles) == 131
    assert len(contexts) == 41
    for item in traders:
        row = source_cycles[item["source_line"] - 1]
        assert item["due"] == row["simulation_completed_at_ms"]
        assert item["due"] >= row["decision_time_ms"]
    assert all(item["due"] >= item["decision"] for item in contexts)


def test_recorded_agenda_rejects_future_trader_output(tmp_path):
    row = json.loads((SOURCE / "cycles.jsonl").read_text().splitlines()[0])
    row["simulation_completed_at_ms"] = row["decision_time_ms"] - 1
    (tmp_path / "cycles.jsonl").write_text(json.dumps(row) + "\n")
    (tmp_path / "role_runs").mkdir()
    packet = tmp_path / row["frozen_packet_file"]
    packet.parent.mkdir()
    packet.write_text("{}")
    with pytest.raises(ValueError, match="timing"):
        _scheduler(tmp_path)._schedule_recorded_outputs()


@pytest.mark.asyncio
async def test_recorded_output_arrives_during_pm_clock_advance():
    runner = _scheduler(SOURCE)
    start = runner.start
    runner.now = start
    runner.minute_index = 0
    runner.minute_bars = [
        {"open_time_ms": start + offset, "close_time_ms": start + offset + 59999}
        for offset in (0, 60000)
    ]
    runner.agenda = [{"kind": "recorded_trader", "due": start + 30000, "sequence": 1}]
    seen = []

    async def apply(item, *, drain_events):
        assert not drain_events
        seen.append(("output", runner.now))

    async def poll():
        pass

    async def activate(bar):
        seen.append(("bar", runner.now))

    runner._apply_recorded = apply
    runner._activate_pending_entries = activate
    runner.watcher = SimpleNamespace(poll=poll)
    runner.venue = SimpleNamespace(resolve_executor_bar=lambda bar: None,
        set_market=lambda bar, **kwargs: None)
    # The PM ticker uses stop_on_event=False. Source output must be injected
    # at its own completion, rather than delayed until PM returns.
    await runner._advance_locked(start + 120000)
    assert seen == [("output", start + 30000),
        ("bar", start + 59999), ("bar", start + 119999)]
    assert runner.agenda == []


def test_report_links_source_capture_without_mutating_cycle(tmp_path):
    path = tmp_path / "source_artifacts/role_runs/trader.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": "source-trader", "role": "TRADER", "calls": []}))
    cycle = {"role_runs": [], "source_role_runs": [str(path.relative_to(tmp_path))]}
    assert _role_runs_for_record(cycle, tmp_path, [])[0]["run_id"] == "source-trader"
    assert cycle["role_runs"] == []
