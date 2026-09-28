"""End-to-end tests for Brooks hedge lifecycle execution through Hummingbot."""

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from condor.brooks.execution import ExecutionRejected, HummingbotExecutionPort
from condor.brooks.gm import (
    AccountSnapshot,
    BrooksGM,
    GMPolicy,
    GMRejected,
    VenueRules,
)
from condor.brooks.hedge import (
    HedgeState,
    PositionLeg,
    build_hedge_state,
)


def D(value):
    return Decimal(str(value))


def rules(**changes):
    defaults = dict(
        amount_step=D("0.01"),
        min_amount=D("0.01"),
        min_notional=D("10"),
        max_leverage=5,
    )
    defaults.update(changes)
    return VenueRules(**defaults)


def policy(**changes):
    defaults = dict(
        risk_per_trade_pct=D("0.01"),
        max_positions=2,
        max_gross_exposure_pct=D("3"),
        leverage=2,
        take_profit_r=D("2"),
        time_limit_sec=3600,
        max_trigger_drift_pct=D("0.01"),
        max_snapshot_age_ms=15_000,
        max_intent_age_ms=7_200_000,
    )
    defaults.update(changes)
    return GMPolicy(**defaults)


def make_snapshot(*, at=None, positions=None, **changes):
    now_ms = int(time.time() * 1000) if at is None else at
    defaults = dict(
        as_of_ms=now_ms,
        equity=D(1000),
        available_margin=D(500),
        mark_price=D(100),
        gross_exposure=D(100),
        open_positions=1,
        rules=rules(),
        structure_status="single_main",
        main_position_id="main-1",
        main_executor_id="exec-main-1",
        main_side="LONG",
        main_quantity=D(1),
        position_mode="HEDGE",
        positions=positions,
    )
    defaults.update(changes)
    return AccountSnapshot(**defaults)


class FakeHedgePort:
    def __init__(self):
        self.controller_id = "brooks"
        self.calls = []
        self.position_mode = "HEDGE"
        self.raise_on_hedge = None
        self.next_executor_id = "exec-hedge-1"

    async def get_position_mode(self):
        if isinstance(self.position_mode, Exception):
            raise self.position_mode
        return self.position_mode

    async def open_main(self, **kwargs):
        self.calls.append(("open_main", kwargs))
        return "exec-main-1"

    async def reduce_main(self, **kwargs):
        self.calls.append(("reduce_main", kwargs))
        return "exec-reduce-1"

    async def close_main(self, **kwargs):
        self.calls.append(("close_main", kwargs))
        return kwargs["executor_id"]

    async def execute_hedge(self, **kwargs):
        self.calls.append(("execute_hedge", kwargs))
        if self.raise_on_hedge:
            raise self.raise_on_hedge
        return self.next_executor_id


class FakeHedgeReader:
    def __init__(self, snapshots):
        self.snapshots = (
            list(snapshots) if isinstance(snapshots, (list, tuple)) else [snapshots]
        )
        self.calls = 0

    async def read(self, **kwargs):
        self.calls += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def setup_gm(tmp_path, reader, port=None, gm_policy=None):
    port = port or FakeHedgePort()
    gm_policy = gm_policy or policy()
    gm = BrooksGM(
        account_name="demo",
        connector_name="binance_perpetual",
        state_root=tmp_path,
        policy=gm_policy,
        reader=reader,
        execution=port,
    )
    return gm, port


def seed_trade_binding(
    tmp_path,
    *,
    correlation_id="c1",
    main_position_id="main-1",
    hedge_position_id=None,
    symbol="BTC-USDT",
    main_side="LONG",
    status="submitted",
):
    trade_dir = tmp_path / "trades" / correlation_id
    trade_dir.mkdir(parents=True, exist_ok=True)
    binding = {
        "schema": "condor.brooks.trade-binding.v1",
        "correlation_id": correlation_id,
        "account_name": "demo",
        "connector_name": "binance_perpetual",
        "controller_id": "brooks",
        "symbol": symbol,
        "main_side": main_side,
        "planned_quantity": "1.0",
        "status": status,
        "main_executor_id": "exec-main-1",
        "executor_id": "exec-main-1",
        "main_position_id": main_position_id,
        "hedge_position_id": hedge_position_id,
        "hedge_executor_id": "exec-hedge-1" if hedge_position_id else None,
    }
    binding_path = trade_dir / "binding.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    return binding


# =========================================================================
# 1. Execution Port Primitives
# =========================================================================


def test_hummingbot_port_execute_hedge_primitives(monkeypatch):
    calls = []

    async def mock_order_executor(client, **kwargs):
        calls.append(kwargs)
        return {"executor_id": "ord-1"}

    monkeypatch.setattr(
        "condor.brooks.execution.executor_create.create_order_executor",
        mock_order_executor,
    )

    port = HummingbotExecutionPort(
        object(),
        account_name="demo",
        connector_name="binance_perpetual",
        controller_id="brooks",
    )

    # OPEN hedge: main LONG -> hedge SELL (side 2)
    exec_id = asyncio.run(
        port.execute_hedge(
            symbol="BTC-USDT",
            side="SELL",
            quantity=D("0.3"),
            position_action="OPEN",
            leverage=2,
        )
    )
    assert exec_id == "ord-1"
    assert calls[-1]["side"] == 2
    assert calls[-1]["position_action"] == "OPEN"
    assert calls[-1]["amount"] == "0.3"
    assert calls[-1]["trading_pair"] == "BTC-USDT"
    assert calls[-1]["execution_strategy"] == "MARKET"

    # CLOSE hedge: buy back (side 1)
    exec_id = asyncio.run(
        port.execute_hedge(
            symbol="BTC-USDT",
            side="BUY",
            quantity=D("0.3"),
            position_action="CLOSE",
            leverage=2,
        )
    )
    assert exec_id == "ord-1"
    assert calls[-1]["side"] == 1
    assert calls[-1]["position_action"] == "CLOSE"

    # Invalid arguments
    with pytest.raises(ValueError, match="symbol"):
        asyncio.run(
            port.execute_hedge(
                symbol="",
                side="BUY",
                quantity=D("0.1"),
                position_action="OPEN",
                leverage=2,
            )
        )

    with pytest.raises(ValueError, match="side"):
        asyncio.run(
            port.execute_hedge(
                symbol="BTC-USDT",
                side="INVALID",
                quantity=D("0.1"),
                position_action="OPEN",
                leverage=2,
            )
        )

    with pytest.raises(ValueError, match="position_action"):
        asyncio.run(
            port.execute_hedge(
                symbol="BTC-USDT",
                side="BUY",
                quantity=D("0.1"),
                position_action="INVALID",
                leverage=2,
            )
        )

    with pytest.raises(ValueError, match="leverage"):
        asyncio.run(
            port.execute_hedge(
                symbol="BTC-USDT",
                side="BUY",
                quantity=D("0.1"),
                position_action="OPEN",
                leverage=0,
            )
        )


def test_hummingbot_port_get_position_mode():
    class ClientTrading:
        def __init__(self, mode="HEDGE"):
            self.mode = mode

        async def get_position_mode(self, **kwargs):
            if self.mode == "error":
                return {"error": "failed to query"}
            return {"position_mode": self.mode}

    class FakeClient:
        def __init__(self, mode="HEDGE"):
            self.trading = ClientTrading(mode)

    port = HummingbotExecutionPort(
        FakeClient("HEDGE"),
        account_name="demo",
        connector_name="binance_perpetual",
        controller_id="brooks",
    )
    assert asyncio.run(port.get_position_mode()) == "HEDGE"

    port_oneway = HummingbotExecutionPort(
        FakeClient("ONEWAY"),
        account_name="demo",
        connector_name="binance_perpetual",
        controller_id="brooks",
    )
    assert asyncio.run(port_oneway.get_position_mode()) == "ONEWAY"

    port_err = HummingbotExecutionPort(
        FakeClient("error"),
        account_name="demo",
        connector_name="binance_perpetual",
        controller_id="brooks",
    )
    with pytest.raises(ExecutionRejected):
        asyncio.run(port_err.get_position_mode())


# =========================================================================
# 2. Full Hedge Lifecycle (HEDGE -> INCREASE -> REDUCE -> REMOVE)
# =========================================================================


def test_full_hedge_lifecycle_execution(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")

    now = int(time.time() * 1000)
    leg_main = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    pre_hedge_state = build_hedge_state(
        [leg_main], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    # Step 1: HEDGE (target = 0.3)
    # Venue read before write: single main
    snap1 = make_snapshot(at=now - 10, positions=[leg_main])
    # Venue read after write: main + hedge-1 (0.3)
    leg_hedge1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    snap1_reconciled = make_snapshot(
        at=now, positions=[leg_main, leg_hedge1], hedge_position_id="hedge-1"
    )

    reader = FakeHedgeReader([snap1, snap1_reconciled])
    port = FakeHedgePort()
    port.next_executor_id = "exec-h1"
    gate, _ = setup_gm(tmp_path, reader, port=port)

    res1 = asyncio.run(
        gate.execute_management(
            correlation_id="c1",
            decision_id="d1",
            action="HEDGE",
            target_hedge_ratio="0.3",
            expected_state=pre_hedge_state,
        )
    )
    assert res1["status"] == "submitted"
    assert res1["executor_id"] == "exec-h1"
    assert res1["quantity"] == "0.30"
    assert port.calls[-1] == (
        "execute_hedge",
        {
            "symbol": "BTC-USDT",
            "side": "SELL",
            "quantity": D("0.30"),
            "position_action": "OPEN",
            "leverage": 2,
        },
    )

    # Check persistence
    binding_data = json.loads(
        (tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8")
    )
    assert Decimal(binding_data["hedge_size"]) == D("0.3")

    saved_state = json.loads(
        (tmp_path / "trades/c1/hedge_state.json").read_text(encoding="utf-8")
    )
    assert saved_state["structure_status"] == "ok"
    assert Decimal(saved_state["hedge_ratio"]) == D("0.3")

    # Step 2: INCREASE_HEDGE (target = 0.5)
    now2 = int(time.time() * 1000)
    active_hedge_state = build_hedge_state(
        [leg_main, leg_hedge1],
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        as_of_ms=now2 - 10,
    )
    snap2 = make_snapshot(
        at=now2 - 5,
        positions=[leg_main, leg_hedge1],
        hedge_position_id="hedge-1",
    )
    leg_hedge2 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    snap2_reconciled = make_snapshot(
        at=now2, positions=[leg_main, leg_hedge2], hedge_position_id="hedge-1"
    )

    reader.snapshots = [snap2, snap2_reconciled]
    port.next_executor_id = "exec-h2"

    res2 = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d2",
            action="INCREASE_HEDGE",
            target_hedge_ratio="0.5",
            expected_state=active_hedge_state,
        )
    )
    assert res2["status"] == "submitted"
    assert res2["quantity"] == "0.20"
    assert port.calls[-1][1]["quantity"] == D("0.20")
    assert port.calls[-1][1]["position_action"] == "OPEN"

    # Step 3: REDUCE_HEDGE (target = 0.2)
    now3 = int(time.time() * 1000)
    state2 = build_hedge_state(
        [leg_main, leg_hedge2],
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        as_of_ms=now3 - 10,
    )
    snap3 = make_snapshot(
        at=now3 - 5,
        positions=[leg_main, leg_hedge2],
        hedge_position_id="hedge-1",
    )
    leg_hedge3 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.2", "100", "HEDGE")
    snap3_reconciled = make_snapshot(
        at=now3, positions=[leg_main, leg_hedge3], hedge_position_id="hedge-1"
    )

    reader.snapshots = [snap3, snap3_reconciled]
    port.next_executor_id = "exec-h3"

    res3 = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d3",
            action="REDUCE_HEDGE",
            target_hedge_ratio="0.2",
            expected_state=state2,
        )
    )
    assert res3["status"] == "submitted"
    assert res3["quantity"] == "0.30"
    assert port.calls[-1][1]["side"] == "BUY"
    assert port.calls[-1][1]["position_action"] == "CLOSE"

    # Step 4: REMOVE_HEDGE (target = 0)
    now4 = int(time.time() * 1000)
    state3 = build_hedge_state(
        [leg_main, leg_hedge3],
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        as_of_ms=now4 - 10,
    )
    snap4 = make_snapshot(
        at=now4 - 5,
        positions=[leg_main, leg_hedge3],
        hedge_position_id="hedge-1",
    )
    snap4_reconciled = make_snapshot(
        at=now4, positions=[leg_main], hedge_position_id=None
    )

    reader.snapshots = [snap4, snap4_reconciled]
    port.next_executor_id = "exec-h4"

    res4 = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d4",
            action="REMOVE_HEDGE",
            target_hedge_ratio="0",
            expected_state=state3,
        )
    )
    assert res4["status"] == "submitted"
    assert res4["quantity"] == "0.20"
    assert port.calls[-1][1]["side"] == "BUY"
    assert port.calls[-1][1]["position_action"] == "CLOSE"

    # Binding should have no hedge position now
    final_binding = json.loads(
        (tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8")
    )
    assert final_binding["hedge_position_id"] is None
    assert final_binding["hedge_size"] == "0"


# =========================================================================
# 3. Mandatory Adversarial Tests (Section 28)
# =========================================================================


def test_adversarial_duplicate_main_rejected_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    m2 = PositionLeg("main-2", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    snap = make_snapshot(at=now, positions=[m1, m2])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="rebuild|duplicate_main"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_duplicate_hedge_rejected_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path,
        correlation_id="c1",
        main_position_id="main-1",
        hedge_position_id="hedge-1",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    h2 = PositionLeg("hedge-2", "BTC-USDT", "SHORT", "0.2", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1],
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )

    snap = make_snapshot(at=now, positions=[m1, h1, h2])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="rebuild|duplicate_hedge"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="INCREASE_HEDGE",
                target_hedge_ratio="0.6",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_orphan_hedge_rejected_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    snap = make_snapshot(at=now, positions=[h1])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="rebuild|orphan_hedge"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_unknown_role_rejected_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    unknown = PositionLeg("unknown-1", "BTC-USDT", "SHORT", "0.3", "100", None)
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    snap = make_snapshot(at=now, positions=[m1, unknown])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="rebuild|unknown_role"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_same_side_legs_rejected_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path,
        correlation_id="c1",
        main_position_id="main-1",
        hedge_position_id="hedge-1",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    same_side_hedge = PositionLeg("hedge-1", "BTC-USDT", "LONG", "0.3", "100", "HEDGE")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1],
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )

    snap = make_snapshot(at=now, positions=[m1, same_side_hedge])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="rebuild|inconsistent"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="INCREASE_HEDGE",
                target_hedge_ratio="0.5",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


@pytest.mark.parametrize("bad_ratio", ["-0.1", "1.1", "NaN", "01", "foo", "0."])
def test_adversarial_invalid_ratio_rejected_no_write(tmp_path, bad_ratio):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )
    snap = make_snapshot(at=now, positions=[m1])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio=bad_ratio,
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


@pytest.mark.parametrize(
    "action,target,active",
    [
        ("REMOVE_HEDGE", "0.1", True),
        ("INCREASE_HEDGE", "0.3", True),
        ("INCREASE_HEDGE", "0.2", True),
        ("REDUCE_HEDGE", "0.3", True),
        ("REDUCE_HEDGE", "0.4", True),
        ("REDUCE_HEDGE", "0", True),
        ("HEDGE", "0.5", True),  # already active
        ("HEDGE", "0", False),
    ],
)
def test_adversarial_invalid_action_target_relation_rejected_no_write(
    tmp_path, action, target, active
):
    seed_trade_binding(
        tmp_path,
        correlation_id="c1",
        main_position_id="main-1",
        hedge_position_id="hedge-1" if active else None,
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    legs = [m1, h1] if active else [m1]
    exp = build_hedge_state(
        legs,
        main_position_id="main-1",
        hedge_position_id="hedge-1" if active else None,
        as_of_ms=now - 50,
    )
    snap = make_snapshot(
        at=now,
        positions=legs,
        hedge_position_id="hedge-1" if active else None,
    )
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action=action,
                target_hedge_ratio=target,
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_stale_snapshot_rejected_no_write(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now
    )

    # Fresh state has same or older timestamp
    snap = make_snapshot(at=now, positions=[m1])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="stale"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()

    # Also test snapshot older than policy.max_snapshot_age_ms
    old_snap = make_snapshot(at=now - 20_000, positions=[m1])
    reader_old = FakeHedgeReader(old_snap)
    gate_old, port_old = setup_gm(tmp_path, reader_old)

    with pytest.raises(GMRejected, match="stale"):
        asyncio.run(
            gate_old.execute_hedge(
                correlation_id="c1",
                decision_id="d2",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=replace(exp, as_of_ms=now - 25_000),
            )
        )
    assert len(port_old.calls) == 0


def test_adversarial_venue_state_changed_before_write_rejected_no_write(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1_before = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1_before],
        main_position_id="main-1",
        hedge_position_id=None,
        as_of_ms=now - 50,
    )

    # Main position quantity or mark price changed on venue before write
    m1_after = PositionLeg("main-1", "BTC-USDT", "LONG", "0.8", "100", "MAIN")
    snap = make_snapshot(at=now, positions=[m1_after])
    reader = FakeHedgeReader(snap)
    gate, port = setup_gm(tmp_path, reader)

    with pytest.raises(GMRejected, match="stale"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_hedge_mode_unconfirmed_fails_closed_no_write(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    # Position mode in snapshot is ONEWAY
    snap = make_snapshot(at=now, positions=[m1], position_mode="ONEWAY")
    port = FakeHedgePort()
    port.position_mode = "ONEWAY"
    reader = FakeHedgeReader(snap)
    gate, _ = setup_gm(tmp_path, reader, port=port)

    with pytest.raises(GMRejected, match="HEDGE position mode"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port.calls) == 0
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_adversarial_partial_fill_fails_closed_into_reconciliation_required(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    snap_pre = make_snapshot(at=now - 10, positions=[m1])
    # Post-write fill is partial: only 0.1 filled instead of 0.3
    h_partial = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.1", "100", "HEDGE")
    snap_post = make_snapshot(
        at=now,
        positions=[m1, h_partial],
        hedge_position_id="hedge-1",
        filled_quantity="0.1",
    )

    reader = FakeHedgeReader([snap_pre, snap_post])
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    with pytest.raises(GMRejected, match="reconciliation"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )

    # Proves status recorded as reconciliation_required and binding updated
    record = json.loads(
        (tmp_path / "trades/c1/management/d1.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "reconciliation_required"
    assert record["assessment_status"] == "partial"

    binding = json.loads(
        (tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8")
    )
    assert binding["status"] == "reconciliation_required"

    # Subsequent write blocked
    with pytest.raises(GMRejected, match="reconcile"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d2",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )


def test_adversarial_ambiguous_execution_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_RECORROBORATE_DELAY_SEC", 0)
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    snap_pre = make_snapshot(at=now - 10, positions=[m1])
    snap_post = make_snapshot(at=now, positions=[m1])  # no newer hedge leg

    reader = FakeHedgeReader([snap_pre, snap_post])
    port = FakeHedgePort()
    port.raise_on_hedge = TimeoutError("gateway timeout")
    gate, _ = setup_gm(tmp_path, reader, port=port)

    with pytest.raises(TimeoutError):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )

    record = json.loads(
        (tmp_path / "trades/c1/management/d1.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "reconciliation_required"
    assert record["assessment_status"] == "ambiguous"

    # Subsequent write is blocked
    with pytest.raises(GMRejected, match="reconcile"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d2",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )


def test_increase_exact_delta_confirms_without_corroboration(tmp_path):
    seed_trade_binding(
        tmp_path, correlation_id="c1", main_position_id="main-1",
        hedge_position_id="hedge-1", status="reconciled",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    h2 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1], main_position_id="main-1", hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )
    snap_pre = make_snapshot(at=now - 10, positions=[m1, h1], hedge_position_id="hedge-1")
    snap_rec = make_snapshot(at=now, positions=[m1, h2], hedge_position_id="hedge-1")
    reader = FakeHedgeReader([snap_pre, snap_rec])
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    res = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1", decision_id="d1", action="INCREASE_HEDGE",
            target_hedge_ratio="0.5", expected_state=exp,
        )
    )
    assert res["status"] == "submitted"
    assert res["assessment"] == "confirmed"
    assert res["filled_quantity"] == "0.2"
    assert reader.calls == 2  # fresh + one reconciling read; no retry needed
    binding = json.loads((tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8"))
    assert binding["status"] == "reconciled"


def test_transient_phantom_read_corroborated_to_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_RECORROBORATE_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path, correlation_id="c1", main_position_id="main-1",
        hedge_position_id="hedge-1", status="reconciled",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    h_phantom = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.7", "100", "HEDGE")
    h2 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1], main_position_id="main-1", hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )
    snap_pre = make_snapshot(at=now - 30, positions=[m1, h1], hedge_position_id="hedge-1")
    snap_phantom = make_snapshot(at=now - 20, positions=[m1, h_phantom], hedge_position_id="hedge-1")
    snap_clean = make_snapshot(at=now - 10, positions=[m1, h2], hedge_position_id="hedge-1")
    reader = FakeHedgeReader([snap_pre, snap_phantom, snap_clean])
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    res = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1", decision_id="d1", action="INCREASE_HEDGE",
            target_hedge_ratio="0.5", expected_state=exp,
        )
    )
    assert res["status"] == "submitted"
    assert res["assessment"] == "confirmed"
    assert res["filled_quantity"] == "0.2"
    assert reader.calls == 3  # fresh + transient + corroborating read
    binding = json.loads((tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8"))
    assert binding["status"] == "reconciled"
    assert Decimal(binding["hedge_size"]) == D("0.5")


def test_persistent_misread_wedges_and_blocks_later_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_RECORROBORATE_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path, correlation_id="c1", main_position_id="main-1",
        hedge_position_id="hedge-1", status="reconciled",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    h_phantom = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.7", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1], main_position_id="main-1", hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )
    snaps = [make_snapshot(at=now - 30, positions=[m1, h1], hedge_position_id="hedge-1")]
    snaps += [
        make_snapshot(at=now - 20 + 10 * i, positions=[m1, h_phantom], hedge_position_id="hedge-1")
        for i in range(3)
    ]
    reader = FakeHedgeReader(snaps)
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    with pytest.raises(GMRejected, match="reconciliation"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1", decision_id="d1", action="INCREASE_HEDGE",
                target_hedge_ratio="0.5", expected_state=exp,
            )
        )
    assert reader.calls == 8  # fresh + initial + 6 bounded corroborating reads
    record = json.loads((tmp_path / "trades/c1/management/d1.json").read_text(encoding="utf-8"))
    assert record["status"] == "reconciliation_required"
    assert record["assessment_status"] == "ambiguous"
    binding = json.loads((tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8"))
    assert binding["status"] == "reconciliation_required"
    with pytest.raises(GMRejected, match="reconcile"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1", decision_id="d2", action="INCREASE_HEDGE",
                target_hedge_ratio="0.5", expected_state=exp,
            )
        )


def test_fresh_split_rows_resolve_on_reread_then_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path, correlation_id="c1", main_position_id="main-1",
        hedge_position_id="hedge-1", status="reconciled",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    h2 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1], main_position_id="main-1", hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )
    snap_split = make_snapshot(at=now - 5, positions=[m1], hedge_position_id="hedge-1")
    snap_ok = make_snapshot(at=now - 3, positions=[m1, h1], hedge_position_id="hedge-1")
    snap_rec = make_snapshot(at=now - 1, positions=[m1, h2], hedge_position_id="hedge-1")
    reader = FakeHedgeReader([snap_split, snap_ok, snap_rec])
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    res = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1", decision_id="d1", action="INCREASE_HEDGE",
            target_hedge_ratio="0.5", expected_state=exp,
        )
    )
    assert res["status"] == "submitted"
    assert res["assessment"] == "confirmed"
    assert reader.calls == 3  # split read, converged read, reconciling read
    assert [call for call, _ in port.calls] == ["execute_hedge"]


def test_fresh_persistently_unresolved_wedges_without_write(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path, correlation_id="c1", main_position_id="main-1",
        hedge_position_id="hedge-1", status="reconciled",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1], main_position_id="main-1", hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )
    snap_split = make_snapshot(at=now - 5, positions=[m1], hedge_position_id="hedge-1")
    reader = FakeHedgeReader([snap_split])
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    with pytest.raises(GMRejected, match="hedge structure unresolved"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1", decision_id="d1", action="INCREASE_HEDGE",
                target_hedge_ratio="0.5", expected_state=exp,
            )
        )
    assert reader.calls == 6  # bounded fresh re-reads, then fail closed
    assert port.calls == []
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_increase_keeps_creator_hedge_executor_id(tmp_path, monkeypatch):
    monkeypatch.setattr("condor.brooks.gm._HEDGE_FRESH_DELAY_SEC", 0)
    monkeypatch.setattr("condor.brooks.gm._HEDGE_RECORROBORATE_DELAY_SEC", 0)
    seed_trade_binding(
        tmp_path, correlation_id="c1", main_position_id="main-1",
        hedge_position_id="hedge-1", status="reconciled",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    h2 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    exp = build_hedge_state(
        [m1, h1], main_position_id="main-1", hedge_position_id="hedge-1",
        as_of_ms=now - 50,
    )
    snap_pre = make_snapshot(at=now - 10, positions=[m1, h1], hedge_position_id="hedge-1")
    snap_rec = make_snapshot(at=now, positions=[m1, h2], hedge_position_id="hedge-1")
    reader = FakeHedgeReader([snap_pre, snap_rec])
    port = FakeHedgePort()
    port.next_executor_id = "exec-h2"
    gate, _ = setup_gm(tmp_path, reader, port=port)

    res = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1", decision_id="d1", action="INCREASE_HEDGE",
            target_hedge_ratio="0.5", expected_state=exp,
        )
    )
    assert res["status"] == "submitted"
    assert res["executor_id"] == "exec-h2"
    binding = json.loads((tmp_path / "trades/c1/binding.json").read_text(encoding="utf-8"))
    assert binding["hedge_position_id"] == "hedge-1"
    assert binding["hedge_executor_id"] == "exec-hedge-1"
    assert Decimal(binding["hedge_size"]) == D("0.5")


def test_adversarial_order_failure_leaves_venue_unchanged(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    snap_pre = make_snapshot(at=now - 10, positions=[m1])
    snap_post = make_snapshot(at=now, positions=[m1])  # unchanged venue

    reader = FakeHedgeReader([snap_pre, snap_post])
    port = FakeHedgePort()
    port.raise_on_hedge = ExecutionRejected("insufficient balance")
    gate, _ = setup_gm(tmp_path, reader, port=port)

    with pytest.raises(GMRejected, match="failed"):
        asyncio.run(
            gate.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )

    record = json.loads(
        (tmp_path / "trades/c1/management/d1.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "failed"


# =========================================================================
# 4. Restart with MAIN + HEDGE Open
# =========================================================================


def test_restart_with_open_main_and_hedge(tmp_path):
    # Simulate existing persisted state from prior process run
    seed_trade_binding(
        tmp_path,
        correlation_id="c1",
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        status="submitted",
    )
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")
    saved_hedge_state = build_hedge_state(
        [m1, h1],
        main_position_id="main-1",
        hedge_position_id="hedge-1",
        as_of_ms=now - 100,
    )
    # Persist saved hedge state
    (tmp_path / "trades/c1/hedge_state.json").write_text(
        json.dumps(
            {
                "schema": saved_hedge_state.schema,
                "structure_status": saved_hedge_state.structure_status,
                "unresolved": saved_hedge_state.unresolved,
                "main_position_id": saved_hedge_state.main_position_id,
                "hedge_position_id": saved_hedge_state.hedge_position_id,
                "symbol": saved_hedge_state.symbol,
                "main_side": saved_hedge_state.main_side,
                "hedge_side": saved_hedge_state.hedge_side,
                "main_size": saved_hedge_state.main_size,
                "hedge_size": saved_hedge_state.hedge_size,
                "hedge_ratio": saved_hedge_state.hedge_ratio,
                "ratio_basis": saved_hedge_state.ratio_basis,
                "net_exposure_usd": saved_hedge_state.net_exposure_usd,
                "gross_exposure_usd": saved_hedge_state.gross_exposure_usd,
                "main_mark_notional": saved_hedge_state.main_mark_notional,
                "hedge_mark_notional": saved_hedge_state.hedge_mark_notional,
                "mark_price": saved_hedge_state.mark_price,
                "hedge_mark_price": saved_hedge_state.hedge_mark_price,
                "as_of_ms": saved_hedge_state.as_of_ms,
                "fingerprint": saved_hedge_state.fingerprint,
            }
        ),
        encoding="utf-8",
    )

    # Fresh process GM instance
    snap_pre = make_snapshot(
        at=now - 10,
        positions=[m1, h1],
        hedge_position_id="hedge-1",
    )
    h_reduced = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.1", "100", "HEDGE")
    snap_post = make_snapshot(
        at=now,
        positions=[m1, h_reduced],
        hedge_position_id="hedge-1",
    )

    reader = FakeHedgeReader([snap_pre, snap_post])
    port = FakeHedgePort()
    gate, _ = setup_gm(tmp_path, reader, port=port)

    # Action after restart: REDUCE_HEDGE to 0.1 (auto-loads saved hedge_state.json)
    res = asyncio.run(
        gate.execute_hedge(
            correlation_id="c1",
            decision_id="d_after_restart",
            action="REDUCE_HEDGE",
            target_hedge_ratio="0.1",
        )
    )
    assert res["status"] == "submitted"
    assert port.calls[-1] == (
        "execute_hedge",
        {
            "symbol": "BTC-USDT",
            "side": "BUY",
            "quantity": D("0.20"),
            "position_action": "CLOSE",
            "leverage": 2,
        },
    )


# =========================================================================
# 5. Risk and Margin Admission Rejections
# =========================================================================


def test_risk_margin_admission_rejections_no_write(tmp_path):
    seed_trade_binding(tmp_path, correlation_id="c1", main_position_id="main-1")
    now = int(time.time() * 1000)
    m1 = PositionLeg("main-1", "BTC-USDT", "LONG", "1.0", "100", "MAIN")
    exp = build_hedge_state(
        [m1], main_position_id="main-1", hedge_position_id=None, as_of_ms=now - 50
    )

    # Case 1: Insufficient available margin
    snap_no_margin = make_snapshot(
        at=now, positions=[m1], available_margin=D("5")
    )  # delta 0.3 * 100 / 2 = 15 required
    reader1 = FakeHedgeReader(snap_no_margin)
    gate1, port1 = setup_gm(tmp_path, reader1)
    with pytest.raises(GMRejected, match="available margin"):
        asyncio.run(
            gate1.execute_hedge(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port1.calls) == 0

    # Case 2: Gross exposure cap exceeded
    snap_high_gross = make_snapshot(
        at=now,
        positions=[m1],
        gross_exposure=D("2990"),
        equity=D(1000),  # max gross pct is 3 (cap is 3000)
    )
    reader2 = FakeHedgeReader(snap_high_gross)
    gate2, port2 = setup_gm(tmp_path, reader2)
    with pytest.raises(GMRejected, match="gross exposure"):
        asyncio.run(
            gate2.execute_hedge(
                correlation_id="c1",
                decision_id="d2",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port2.calls) == 0

    # Case 3: Delta notional below venue min_notional
    snap_low_notional = make_snapshot(
        at=now,
        positions=[m1],
        rules=rules(min_notional=D("50")),  # delta is 30
    )
    reader3 = FakeHedgeReader(snap_low_notional)
    gate3, port3 = setup_gm(tmp_path, reader3)
    with pytest.raises(GMRejected, match="minimum notional"):
        asyncio.run(
            gate3.execute_hedge(
                correlation_id="c1",
                decision_id="d3",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port3.calls) == 0

    # Case 4: Pending orders present
    snap_pending = make_snapshot(at=now, positions=[m1], pending_orders=True)
    reader4 = FakeHedgeReader(snap_pending)
    gate4, port4 = setup_gm(tmp_path, reader4)
    with pytest.raises(GMRejected):
        asyncio.run(
            gate4.execute_hedge(
                correlation_id="c1",
                decision_id="d4",
                action="HEDGE",
                target_hedge_ratio="0.3",
                expected_state=exp,
            )
        )
    assert len(port4.calls) == 0


def test_short_main_hedge_lifecycle(tmp_path):
    seed_trade_binding(
        tmp_path,
        correlation_id="c_short",
        main_position_id="main-s",
        main_side="SHORT",
    )
    now = int(time.time() * 1000)
    m_short = PositionLeg("main-s", "BTC-USDT", "SHORT", "1.0", "100", "MAIN")
    pre_state = build_hedge_state(
        [m_short], main_position_id="main-s", hedge_position_id=None, as_of_ms=now - 20
    )

    snap_pre = make_snapshot(
        at=now - 10,
        positions=[m_short],
        main_position_id="main-s",
        main_side="SHORT",
    )
    h_long = PositionLeg("hedge-long", "BTC-USDT", "LONG", "0.3", "100", "HEDGE")
    snap_post = make_snapshot(
        at=now,
        positions=[m_short, h_long],
        main_position_id="main-s",
        main_side="SHORT",
        hedge_position_id="hedge-long",
    )

    reader = FakeHedgeReader([snap_pre, snap_post])
    port = FakeHedgePort()
    port.next_executor_id = "exec-h-long"
    gate, _ = setup_gm(tmp_path, reader, port=port)

    # For SHORT main, opening hedge must BUY (side = BUY)
    res = asyncio.run(
        gate.execute_hedge(
            correlation_id="c_short",
            decision_id="d_open_long_hedge",
            action="HEDGE",
            target_hedge_ratio="0.3",
            expected_state=pre_state,
        )
    )
    assert res["status"] == "submitted"
    assert port.calls[-1] == (
        "execute_hedge",
        {
            "symbol": "BTC-USDT",
            "side": "BUY",
            "quantity": D("0.30"),
            "position_action": "OPEN",
            "leverage": 2,
        },
    )

    # Now remove hedge: closing hedge on SHORT main must SELL (side = SELL)
    now2 = int(time.time() * 1000)
    active_state = build_hedge_state(
        [m_short, h_long],
        main_position_id="main-s",
        hedge_position_id="hedge-long",
        as_of_ms=now2 - 20,
    )
    snap2_pre = make_snapshot(
        at=now2 - 10,
        positions=[m_short, h_long],
        main_position_id="main-s",
        main_side="SHORT",
        hedge_position_id="hedge-long",
    )
    snap2_post = make_snapshot(
        at=now2,
        positions=[m_short],
        main_position_id="main-s",
        main_side="SHORT",
        hedge_position_id=None,
    )

    reader.snapshots = [snap2_pre, snap2_post]
    port.next_executor_id = "exec-close-h-long"

    res_remove = asyncio.run(
        gate.execute_hedge(
            correlation_id="c_short",
            decision_id="d_remove_long_hedge",
            action="REMOVE_HEDGE",
            target_hedge_ratio="0",
            expected_state=active_state,
        )
    )
    assert res_remove["status"] == "submitted"
    assert port.calls[-1] == (
        "execute_hedge",
        {
            "symbol": "BTC-USDT",
            "side": "SELL",
            "quantity": D("0.30"),
            "position_action": "CLOSE",
            "leverage": 2,
        },
    )
