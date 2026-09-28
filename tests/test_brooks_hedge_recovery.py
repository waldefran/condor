"""Explicit, read-only recovery from ``reconciliation_required``.

The write path wedges a hedge at ``reconciliation_required`` when the
corroborating reads cannot classify the outcome. These tests pin the recovery
contract: fresh venue reads rebuild MAIN/HEDGE ownership, the pending
management record is re-evaluated against the delta it recorded, and only two
independent agreeing reads update the record, hedge state and binding. The
original order is never retried and persistent ambiguity stays blocked.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import pytest

from condor.brooks.adapters import HummingbotAccountReader
from condor.brooks.gm import BrooksGM, GMRejected
from condor.brooks.hedge import PositionLeg
from tests.test_brooks_hedge_execution import (
    D,
    FakeHedgePort,
    FakeHedgeReader,
    make_snapshot,
    policy,
    setup_gm,
)
from tests.test_brooks_reconciliation import (
    ACCOUNT,
    CONNECTOR,
    CONTROLLER,
    SYMBOL,
    FakeClient,
    FakePort,
    venue_position,
)


def rule_delay(monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_RECOVERY_DELAY_SEC", 0)


def seed_reconciliation_required(
    tmp_path,
    *,
    correlation_id="c1",
    account_name="demo",
    connector_name="binance_perpetual",
    controller_id="brooks",
    symbol="BTC-USDT",
    main_position_id="main-1",
    main_side="LONG",
    hedge_position_id=None,
    hedge_size=None,
    main_executor_id="exec-main-1",
    hedge_executor_id=None,
):
    trade_dir = tmp_path / "trades" / correlation_id
    trade_dir.mkdir(parents=True, exist_ok=True)
    binding = {
        "schema": "condor.brooks.trade-binding.v1",
        "correlation_id": correlation_id,
        "account_name": account_name,
        "connector_name": connector_name,
        "controller_id": controller_id,
        "symbol": symbol,
        "main_side": main_side,
        "planned_quantity": "1.0",
        "status": "reconciliation_required",
        "main_executor_id": main_executor_id,
        "executor_id": main_executor_id,
        "main_position_id": main_position_id,
        "hedge_position_id": hedge_position_id,
        "hedge_executor_id": hedge_executor_id,
    }
    if hedge_size is not None:
        binding["hedge_size"] = hedge_size
    (trade_dir / "binding.json").write_text(json.dumps(binding), encoding="utf-8")
    return binding


def seed_pending_record(
    tmp_path,
    *,
    correlation_id="c1",
    decision_id="d1",
    action="HEDGE",
    quantity="0.5",
    expected_hedge_size="0.5",
    position_action="OPEN",
    main_position_id="main-1",
    hedge_position_id=None,
    executor_id="exec-h1",
    target_hedge_ratio="0.5",
):
    mgmt = tmp_path / "trades" / correlation_id / "management"
    mgmt.mkdir(parents=True, exist_ok=True)
    record = {
        "decision_id": decision_id,
        "action": action,
        "target_hedge_ratio": target_hedge_ratio,
        "quantity": quantity,
        "position_action": position_action,
        "status": "reconciliation_required",
        "created_at_ms": 0,
        "expected_hedge_size": expected_hedge_size,
        "state_fingerprint": "fp",
        "state_as_of_ms": 0,
        "main_position_id": main_position_id,
        "hedge_position_id": hedge_position_id,
        "executor_id": executor_id,
        "assessment_status": "ambiguous",
    }
    (mgmt / f"{decision_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_records(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_ambiguous_recovery_confirms_when_venue_catches_up(tmp_path, monkeypatch):
    rule_delay(monkeypatch)
    seed_reconciliation_required(tmp_path)
    seed_pending_record(tmp_path)
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    reader = FakeHedgeReader(
        [
            # Venue lag: the fill is not visible on the first read; a single
            # stale read must not fail the trade.
            make_snapshot(at=now - 30, positions=[m1]),
            make_snapshot(at=now - 20, positions=[m1, h1], hedge_position_id="hedge-1"),
            make_snapshot(at=now - 10, positions=[m1, h1], hedge_position_id="hedge-1"),
        ]
    )
    gate, port = setup_gm(tmp_path, reader)

    res = asyncio.run(gate.reconcile_hedge("c1"))

    assert res["status"] == "confirmed"
    assert res["binding_status"] == "reconciled"
    assert port.calls == []  # never retries the original order
    assert reader.calls == 3  # lagging read, catch-up, confirming read
    binding = read_json(tmp_path / "trades/c1/binding.json")
    assert binding["status"] == "reconciled"
    assert binding["hedge_position_id"] == "hedge-1"
    assert binding["hedge_executor_id"] == "exec-h1"
    assert Decimal(binding["hedge_size"]) == D("0.5")
    record = read_json(tmp_path / "trades/c1/management/d1.json")
    assert record["status"] == "submitted"
    assert record["assessment"] == "confirmed"
    assert Decimal(record["filled_quantity"]) == D("0.5")
    state = read_json(tmp_path / "trades/c1/hedge_state.json")
    assert state["structure_status"] == "ok"
    assert Decimal(state["hedge_size"]) == D("0.5")
    executions = read_records(tmp_path / "trades/c1/executions.jsonl")
    assert executions[-1]["recovered"] is True
    assert executions[-1]["status"] == "confirmed"

    # Idempotent: nothing is pending anymore, nothing is rewritten.
    again = asyncio.run(gate.reconcile_hedge("c1"))
    assert again["status"] == "not_required"

    # New actions are unblocked: a fresh INCREASE_HEDGE runs against the
    # recovered state.
    h2 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.7", "100", "HEDGE")
    base = int(time.time() * 1000)
    reader.snapshots = [
        make_snapshot(at=base - 1, positions=[m1, h1], hedge_position_id="hedge-1"),
        make_snapshot(at=base, positions=[m1, h2], hedge_position_id="hedge-1"),
    ]
    follow = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d2",
            action="INCREASE_HEDGE",
            target_hedge_ratio="0.7",
        )
    )
    assert follow["status"] == "submitted"
    assert follow["quantity"] == "0.20"


def test_partial_recovery_persists_real_state_and_requires_new_delta(
    tmp_path, monkeypatch
):
    rule_delay(monkeypatch)
    seed_reconciliation_required(
        tmp_path, hedge_position_id="hedge-1", hedge_size="0.3"
    )
    seed_pending_record(
        tmp_path,
        action="INCREASE_HEDGE",
        quantity="0.2",
        expected_hedge_size="0.5",
        hedge_position_id="hedge-1",
        executor_id="exec-h2",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h_partial = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.4", "100", "HEDGE")
    reader = FakeHedgeReader(
        [
            make_snapshot(
                at=now - 20, positions=[m1, h_partial], hedge_position_id="hedge-1"
            ),
            make_snapshot(
                at=now - 10, positions=[m1, h_partial], hedge_position_id="hedge-1"
            ),
        ]
    )
    gate, port = setup_gm(tmp_path, reader)

    res = asyncio.run(gate.reconcile_hedge("c1"))

    assert res["status"] == "partial"
    assert port.calls == []
    record = read_json(tmp_path / "trades/c1/management/d1.json")
    assert record["status"] == "partial"
    assert record["assessment"] == "partial"
    assert Decimal(record["filled_quantity"]) == D("0.1")
    binding = read_json(tmp_path / "trades/c1/binding.json")
    assert binding["status"] == "reconciled"
    assert binding["hedge_position_id"] == "hedge-1"
    assert Decimal(binding["hedge_size"]) == D("0.4")
    state = read_json(tmp_path / "trades/c1/hedge_state.json")
    assert Decimal(state["hedge_size"]) == D("0.4")
    assert read_records(tmp_path / "trades/c1/executions.jsonl") == []

    # The real state is the new decision basis: the remaining delta is 0.10.
    h_full = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    base = int(time.time() * 1000)
    reader.snapshots = [
        make_snapshot(
            at=base - 1, positions=[m1, h_partial], hedge_position_id="hedge-1"
        ),
        make_snapshot(at=base, positions=[m1, h_full], hedge_position_id="hedge-1"),
    ]
    follow = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d2",
            action="INCREASE_HEDGE",
            target_hedge_ratio="0.5",
        )
    )
    assert follow["status"] == "submitted"
    assert follow["quantity"] == "0.10"
    assert port.calls[-1][1]["quantity"] == D("0.10")


def test_failed_unchanged_recovery_preserves_state_and_unblocks(tmp_path, monkeypatch):
    rule_delay(monkeypatch)
    seed_reconciliation_required(tmp_path)
    seed_pending_record(tmp_path)
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    reader = FakeHedgeReader(
        [
            make_snapshot(at=now - 20, positions=[m1]),
            make_snapshot(at=now - 10, positions=[m1]),
        ]
    )
    gate, port = setup_gm(tmp_path, reader)

    res = asyncio.run(gate.reconcile_hedge("c1"))

    assert res["status"] == "failed"
    assert port.calls == []
    record = read_json(tmp_path / "trades/c1/management/d1.json")
    assert record["status"] == "failed"
    assert "unchanged" in record["reason"]
    binding = read_json(tmp_path / "trades/c1/binding.json")
    assert binding["status"] == "reconciled"
    assert binding.get("hedge_position_id") is None
    assert not (tmp_path / "trades/c1/hedge_state.json").exists()
    assert read_records(tmp_path / "trades/c1/executions.jsonl") == []

    # New actions are unblocked and the unchanged venue state is re-compiled.
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    base = int(time.time() * 1000)
    reader.snapshots = [
        make_snapshot(at=base - 1, positions=[m1]),
        make_snapshot(at=base, positions=[m1, h1], hedge_position_id="hedge-1"),
    ]
    follow = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d2",
            action="HEDGE",
            target_hedge_ratio="0.5",
        )
    )
    assert follow["status"] == "submitted"
    assert port.calls[-1][1]["quantity"] == D("0.50")


def test_persistent_ambiguity_stays_blocked(tmp_path, monkeypatch):
    rule_delay(monkeypatch)
    seed_reconciliation_required(
        tmp_path, hedge_position_id="hedge-1", hedge_size="0.3"
    )
    seed_pending_record(
        tmp_path,
        action="INCREASE_HEDGE",
        quantity="0.2",
        expected_hedge_size="0.5",
        hedge_position_id="hedge-1",
        executor_id="exec-h2",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h_phantom = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.9", "100", "HEDGE")
    reader = FakeHedgeReader(
        [
            make_snapshot(
                at=now + i,
                positions=[m1, h_phantom],
                hedge_position_id="hedge-1",
            )
            for i in range(6)
        ]
    )
    gate, port = setup_gm(tmp_path, reader)

    res = asyncio.run(gate.reconcile_hedge("c1"))

    assert res["status"] == "ambiguous"
    assert port.calls == []
    binding = read_json(tmp_path / "trades/c1/binding.json")
    assert binding["status"] == "reconciliation_required"
    record = read_json(tmp_path / "trades/c1/management/d1.json")
    assert record["status"] == "reconciliation_required"
    assert not (tmp_path / "trades/c1/hedge_state.json").exists()

    # Writes stay blocked while the outcome is ambiguous.
    with pytest.raises(GMRejected, match="reconcile"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d2",
                action="INCREASE_HEDGE",
                target_hedge_ratio="0.5",
            )
        )
    assert port.calls == []


def test_unacknowledged_first_hedge_cannot_be_failed_by_absence(tmp_path, monkeypatch):
    rule_delay(monkeypatch)
    seed_reconciliation_required(tmp_path)
    seed_pending_record(tmp_path, executor_id=None)
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    reader = FakeHedgeReader(
        [make_snapshot(at=now + i, positions=[m1]) for i in range(6)]
    )
    gate, port = setup_gm(tmp_path, reader)

    # The write was never acknowledged (no executor_id), so an unchanged read
    # cannot prove the order never landed: stay blocked.
    res = asyncio.run(gate.reconcile_hedge("c1"))

    assert res["status"] == "ambiguous"
    assert "lineage" in res["reason"]
    assert port.calls == []
    binding = read_json(tmp_path / "trades/c1/binding.json")
    assert binding["status"] == "reconciliation_required"
    record = read_json(tmp_path / "trades/c1/management/d1.json")
    assert record["status"] == "reconciliation_required"


def test_restart_reconciliation_required_recovers_from_disk_and_venue(
    tmp_path, monkeypatch
):
    rule_delay(monkeypatch)
    seed_reconciliation_required(
        tmp_path,
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        controller_id=CONTROLLER,
        symbol=SYMBOL,
        main_position_id="pos-venue-1",
        main_executor_id="exec-main-1",
        hedge_position_id="pos-hedge-1",
        hedge_executor_id="exec-h1",
        hedge_size="0.4",
    )
    seed_pending_record(
        tmp_path,
        action="INCREASE_HEDGE",
        quantity="0.2",
        expected_hedge_size="0.6",
        hedge_position_id="pos-hedge-1",
        main_position_id="pos-venue-1",
        executor_id="exec-h2",
    )
    fake = FakeClient()
    fake.positions = [
        venue_position("pos-venue-1", "LONG"),
        {
            "position_id": "pos-hedge-1",
            "trading_pair": SYMBOL,
            "position_side": "SHORT",
            "net_amount_base": "0.6",
            "current_price": "100",
        },
    ]
    ticks = iter(range(1, 100))
    reader = HummingbotAccountReader(
        fake, tmp_path, CONTROLLER, now_fn=lambda: 1_000_000 + next(ticks)
    )
    gate = BrooksGM(
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        state_root=tmp_path,
        policy=policy(),
        reader=reader,
        execution=FakePort(),
        reconciler=None,
    )

    # Fresh process over the persisted wedge: production reader must resolve
    # both legs from the reconciliation_required binding.
    res = asyncio.run(gate.reconcile_hedge("c1"))

    assert res["status"] == "confirmed"
    binding = read_json(tmp_path / "trades/c1/binding.json")
    assert binding["status"] == "reconciled"
    assert binding["hedge_position_id"] == "pos-hedge-1"
    assert Decimal(binding["hedge_size"]) == D("0.6")
    record = read_json(tmp_path / "trades/c1/management/d1.json")
    assert record["status"] == "submitted"
    assert record["assessment"] == "confirmed"
    state = read_json(tmp_path / "trades/c1/hedge_state.json")
    assert Decimal(state["hedge_size"]) == D("0.6")
    assert gate.execution.calls == []
