"""Brooks GM and Hummingbot port: deterministic writes without a live venue."""

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from condor.brooks.contracts import HedgePlanV2, ManagementDecisionV2
from condor.brooks.events import BrooksEvent, EventType
from condor.brooks.execution import ExecutionRejected, HummingbotExecutionPort
from condor.brooks.gm import (
    AccountSnapshot,
    BrooksGM,
    GMPolicy,
    GMRejected,
    VenueRules,
    compile_main,
)
from condor.brooks.hedge import PositionLeg, build_hedge_state
from condor.brooks.supervisor import GMConsumer


def D(value):
    return Decimal(str(value))


def intent(decision="ENTER_LONG"):
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": decision,
        "symbol": "BTC-USDT",
        "decision_time_ms": int(time.time() * 1000),
        "trigger": {"price": "100"},
        "invalidation": {"price": "95" if decision == "ENTER_LONG" else "105"},
    }


def policy(**changes):
    return replace(GMPolicy(D("0.01"), 2, D("2"), 2, D("2"), 3600), **changes)


def snapshot(**changes):
    return replace(
        AccountSnapshot(
            as_of_ms=int(time.time() * 1000),
            equity=D(1000),
            available_margin=D(500),
            mark_price=D(100),
            gross_exposure=D(0),
            open_positions=0,
            rules=VenueRules(D("0.01"), D("0.01"), D(10), 5),
        ),
        **changes,
    )


class FakeReader:
    def __init__(self, state):
        self.state = state
        self.calls = 0

    async def read(self, **kwargs):
        self.calls += 1
        return self.state


class FakePort:
    def __init__(self):
        self.controller_id = "brooks"
        self.calls = []
        self.raise_on_open = False
        self.position_mode = "HEDGE"

    async def get_position_mode(self):
        return self.position_mode

    async def open_main(self, **kwargs):
        self.calls.append(("open", kwargs))
        if self.raise_on_open:
            raise TimeoutError("unknown venue outcome")
        return "exec-main"

    async def reduce_main(self, **kwargs):
        self.calls.append(("reduce", kwargs))
        return "exec-reduce"

    async def close_main(self, **kwargs):
        self.calls.append(("close", kwargs))
        return kwargs["executor_id"]

    async def execute_hedge(self, **kwargs):
        self.calls.append(("hedge", kwargs))
        return "exec-hedge"


def gm(tmp_path, state=None, port=None):
    reader = FakeReader(state or snapshot())
    port = port or FakePort()
    return (
        BrooksGM(
            account_name="demo",
            connector_name="binance_perpetual",
            state_root=tmp_path,
            policy=policy(),
            reader=reader,
            execution=port,
        ),
        reader,
        port,
    )


def test_sizing_quantizes_down_and_builds_fractional_barriers():
    plan = compile_main(
        intent(),
        snapshot(mark_price=D("100.5")),
        policy(),
        now_ms=int(time.time() * 1000),
    )
    assert plan.quantity == D("1.81")
    assert plan.quantity * (D("100.5") - D("95")) <= D(10)
    assert plan.stop_loss_pct == D("5.5") / D("100.5")
    assert plan.take_profit_pct == plan.stop_loss_pct * 2
    assert plan.margin_required == plan.notional / 2


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"available_margin": D(1)}, "margin"),
        ({"gross_exposure": D(1900)}, "gross exposure"),
        ({"open_positions": 2}, "max positions"),
        ({"structure_status": "unknown_role"}, "ownership"),
        ({"mark_price": D(110)}, "moved"),
        ({"as_of_ms": 1}, "stale"),
        ({"rules": VenueRules(D("10"), D("0.01"), D(10), 5)}, "minimum"),
        ({"rules": VenueRules(D("0.01"), D("0.01"), D(1000), 5)}, "notional"),
        ({"rules": VenueRules(D("0.01"), D("0.01"), D(10), 1)}, "leverage"),
    ],
)
def test_entry_fails_closed_on_account_and_rule_limits(change, reason):
    with pytest.raises(GMRejected, match=reason):
        compile_main(
            intent(), snapshot(**change), policy(), now_ms=int(time.time() * 1000)
        )


def test_bad_stop_and_no_trade_do_not_open(tmp_path):
    with pytest.raises(GMRejected, match="wrong side"):
        compile_main(
            {**intent(), "invalidation": {"price": "101"}}, snapshot(), policy()
        )
    gate, reader, port = gm(tmp_path)
    assert (
        asyncio.run(gate.execute_entry(intent("NO_TRADE"), correlation_id="c1")) is None
    )
    assert reader.calls == 0 and port.calls == []


def test_entry_persists_original_intent_and_binding_before_write(tmp_path):
    gate, reader, port = gm(tmp_path)
    original = intent()
    result = asyncio.run(gate.execute_entry(original, correlation_id="c1"))
    assert result["main_executor_id"] == "exec-main"
    assert result["status"] == "submitted"
    assert port.calls[0][1]["quantity"] == D(2)
    assert (
        json.loads((tmp_path / "trades/c1/original_trade_intent.json").read_text())
        == original
    )
    with pytest.raises(GMRejected, match="already submitted"):
        asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    assert len(port.calls) == 1 and reader.calls == 1


def test_ambiguous_entry_keeps_reservation_and_never_retries(tmp_path):
    port = FakePort()
    port.raise_on_open = True
    gate, _, _ = gm(tmp_path, port=port)
    with pytest.raises(TimeoutError):
        asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    assert (
        json.loads((tmp_path / "trades/c1/binding.json").read_text())["status"]
        == "submitting"
    )
    with pytest.raises(GMRejected, match="already submitted"):
        asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    assert len(port.calls) == 1


def test_parallel_entries_for_same_symbol_serialize_and_reserve(tmp_path):
    gate, reader, port = gm(tmp_path)

    async def run():
        return await asyncio.gather(
            gate.execute_entry(intent(), correlation_id="a"),
            gate.execute_entry(intent(), correlation_id="b"),
            return_exceptions=True,
        )

    # Reader changes after the first accepted write as a venue would.
    original = port.open_main

    async def open_and_update(**kwargs):
        result = await original(**kwargs)
        reader.state = snapshot(structure_status="single_main", open_positions=1)
        return result

    port.open_main = open_and_update
    result = asyncio.run(run())
    assert len([x for x in result if isinstance(x, dict)]) == 1
    assert len(port.calls) == 1
    assert reader.calls == 1


def test_second_correlation_blocks_while_first_binding_is_unreconciled(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="a"))
    with pytest.raises(GMRejected, match="MAIN already submitted"):
        asyncio.run(gate.execute_entry(intent(), correlation_id="b"))
    assert reader.calls == 1 and len(port.calls) == 1


def test_stale_intent_is_rejected():
    old = {**intent(), "decision_time_ms": 1}
    with pytest.raises(GMRejected, match="stale"):
        compile_main(old, snapshot(), policy())


def managed_state(**changes):
    fields = dict(
        structure_status="single_main",
        open_positions=1,
        main_position_id="position-main",
        main_executor_id="exec-main",
        main_side="LONG",
        main_quantity=D(2),
    )
    fields.update(changes)
    return snapshot(**fields)


def test_management_rereads_ownership_and_quantizes_reduce(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    reader.state = managed_state()
    result = asyncio.run(
        gate.execute_management(
            correlation_id="c1",
            decision_id="d1",
            action="REDUCE",
            reduce_fraction=D("0.255"),
        )
    )
    assert result["quantity"] == "0.51"
    assert port.calls[-1] == (
        "reduce",
        {"symbol": "BTC-USDT", "side": "LONG", "quantity": D("0.51"), "leverage": 2},
    )
    with pytest.raises(GMRejected, match="already submitted"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d1",
                action="REDUCE",
                reduce_fraction=D("0.255"),
            )
        )
    result = asyncio.run(
        gate.execute_management(correlation_id="c1", decision_id="d2", action="CLOSE")
    )
    assert result["status"] == "submitted" and port.calls[-1] == (
        "close",
        {"executor_id": "exec-main"},
    )


def test_management_blocks_changed_state_without_write(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    reader.state = managed_state(main_executor_id="foreign")
    with pytest.raises(GMRejected, match="ownership"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1", decision_id="d1", action="CLOSE"
            )
        )
    assert len(port.calls) == 1
    assert not (tmp_path / "trades/c1/management/d1.json").exists()


def test_hummingbot_port_uses_existing_primitives(monkeypatch):
    calls = []

    async def position(client, **kwargs):
        calls.append(("position", kwargs))
        return {"executor_id": "p1"}

    async def order(client, **kwargs):
        calls.append(("order", kwargs))
        return {"executor_id": "o1"}

    async def stop(client, **kwargs):
        calls.append(("stop", kwargs))
        return {"result": {"status": "stopping"}}

    monkeypatch.setattr(
        "condor.brooks.execution.executor_create.create_position_executor", position
    )
    monkeypatch.setattr(
        "condor.brooks.execution.executor_create.create_order_executor", order
    )
    monkeypatch.setattr("condor.brooks.execution.executors.stop_executor", stop)
    port = HummingbotExecutionPort(
        object(),
        account_name="demo",
        connector_name="binance_perpetual",
        controller_id="brooks",
    )
    assert (
        asyncio.run(
            port.open_main(
                symbol="BTC-USDT",
                side="LONG",
                quantity=D("1.25"),
                leverage=2,
                stop_loss_pct=D("0.05"),
                take_profit_pct=D("0.1"),
                time_limit_sec=3600,
            )
        )
        == "p1"
    )
    assert calls[0][1]["open_order_type"] == 1
    assert calls[0][1]["stop_loss"] == 0.05
    assert calls[0][1]["take_profit"] == 0.1
    assert calls[0][1]["time_limit"] == 3600
    assert (
        asyncio.run(
            port.reduce_main(
                symbol="BTC-USDT", side="LONG", quantity=D("0.5"), leverage=2
            )
        )
        == "o1"
    )
    assert calls[1][1]["side"] == 2 and calls[1][1]["position_action"] == "CLOSE"
    assert asyncio.run(port.close_main(executor_id="p1")) == "p1"
    assert calls[2][1]["keep_position"] is False


def test_hummingbot_port_rejects_ambiguous_response(monkeypatch):
    async def ambiguous(*args, **kwargs):
        return {"formatted_output": "success maybe"}

    monkeypatch.setattr(
        "condor.brooks.execution.executor_create.create_position_executor", ambiguous
    )
    port = HummingbotExecutionPort(
        object(),
        account_name="demo",
        connector_name="binance_perpetual",
        controller_id="brooks",
    )
    with pytest.raises(ExecutionRejected, match="no executor_id"):
        asyncio.run(
            port.open_main(
                symbol="BTC-USDT",
                side="LONG",
                quantity=D(1),
                leverage=2,
                stop_loss_pct=D("0.05"),
                take_profit_pct=D("0.1"),
                time_limit_sec=3600,
            )
        )


def test_management_dispatches_hedge_action(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    now = int(time.time() * 1000)
    m1 = PositionLeg("position-main", "BTC-USDT", "LONG", "2.0", "100", "MAIN")
    pre_state = build_hedge_state(
        [m1],
        main_position_id="position-main",
        hedge_position_id=None,
        as_of_ms=now - 20,
    )
    snap_pre = managed_state(as_of_ms=now - 10, positions=[m1])
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.5", "100", "HEDGE")
    snap_post = managed_state(
        as_of_ms=now,
        positions=[m1, h1],
        hedge_position_id="hedge-1",
        hedge_quantity=D("0.5"),
    )
    snapshots = [snap_pre, snap_post]

    async def multi_read(**kwargs):
        reader.calls += 1
        return snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]

    reader.read = multi_read
    result = asyncio.run(
        gate.execute_management(
            correlation_id="c1",
            decision_id="d_hedge",
            action="HEDGE",
            target_hedge_ratio=D("0.25"),
            expected_state=pre_state,
        )
    )
    assert result["status"] == "submitted"
    assert port.calls[-1] == (
        "hedge",
        {
            "symbol": "BTC-USDT",
            "side": "SELL",
            "quantity": D("0.50"),
            "position_action": "OPEN",
            "leverage": 2,
        },
    )
    assert (tmp_path / "trades/c1/management/d_hedge.json").exists()
    assert (tmp_path / "trades/c1/hedge_state.json").exists()


def test_management_full_decision_pass_through(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))

    # 1. Full decision pass-through for REDUCE
    reader.state = managed_state(main_quantity=D("2.0"))
    decision_reduce = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": int(time.time() * 1000),
        "action": "REDUCE",
        "position_ids": ["position-main"],
        "reason": "thesis calls for de-risking",
        "evidence": {
            "observations": ["pullback"],
            "evidence_for": ["weakening"],
            "evidence_against": ["trend"],
        },
        "risk": {
            "exposure_before": ["long"],
            "exposure_after": ["long partial"],
            "protection_status": "adequate",
            "costs_considered": ["fees"],
            "uncertainty": "low",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": None,
        "market_analysis_request": None,
        "conditions_that_change_action": ["reversal"],
        "reduce_fraction": "0.30",
        "decision_id": "d_full_reduce",
    }
    result_reduce = asyncio.run(
        gate.execute_management(
            correlation_id="c1",
            decision=decision_reduce,
        )
    )
    assert result_reduce["status"] == "submitted"
    assert result_reduce["quantity"] == "0.60"
    assert port.calls[-1] == (
        "reduce",
        {"symbol": "BTC-USDT", "side": "LONG", "quantity": D("0.60"), "leverage": 2},
    )

    # 2. Full decision pass-through for HEDGE
    now = int(time.time() * 1000)
    m1 = PositionLeg("position-main", "BTC-USDT", "LONG", "2.0", "100", "MAIN")
    pre_state = build_hedge_state(
        [m1],
        main_position_id="position-main",
        hedge_position_id=None,
        as_of_ms=now - 20,
    )
    snap_pre = managed_state(as_of_ms=now - 10, positions=[m1], main_quantity=D("2.0"))
    h1 = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.6", "100", "HEDGE")
    snap_post = managed_state(
        as_of_ms=now,
        positions=[m1, h1],
        hedge_position_id="hedge-1",
        hedge_quantity=D("0.6"),
    )
    snapshots = [snap_pre, snap_post]

    async def multi_read(**kwargs):
        reader.calls += 1
        return snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]

    reader.read = multi_read
    decision_hedge = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": int(time.time() * 1000),
        "action": "HEDGE",
        "position_ids": ["position-main"],
        "reason": "thesis calls for hedge protection",
        "evidence": {
            "observations": ["distribution"],
            "evidence_for": ["resistance"],
            "evidence_against": ["trend"],
        },
        "risk": {
            "exposure_before": ["long"],
            "exposure_after": ["hedged"],
            "protection_status": "adequate",
            "costs_considered": ["fees"],
            "uncertainty": "medium",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": {
            "objective": "protect capital",
            "target_hedge_ratio": "0.30",
            "main_position_id": "position-main",
            "hedge_position_id": None,
            "ratio_basis": "absolute_mark_notional",
            "expected_effect_on_exposure": "hedge",
            "costs": ["fees"],
            "unlock_condition": "persistence",
            "failure_condition": "break",
        },
        "market_analysis_request": None,
        "conditions_that_change_action": ["reversal"],
        "decision_id": "d_full_hedge",
    }
    result_hedge = asyncio.run(
        gate.execute_management(
            correlation_id="c1",
            decision=decision_hedge,
            expected_state=pre_state,
        )
    )
    assert result_hedge["status"] == "submitted"
    assert port.calls[-1] == (
        "hedge",
        {
            "symbol": "BTC-USDT",
            "side": "SELL",
            "quantity": D("0.60"),
            "position_action": "OPEN",
            "leverage": 2,
        },
    )
    assert (tmp_path / "trades/c1/management/d_full_hedge.json").exists()


def test_management_reduce_fraction_math(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))

    # Fraction >= 1 rejected
    reader.state = managed_state(main_quantity=D("2.0"))
    with pytest.raises(GMRejected, match="below one"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad1",
                action="REDUCE",
                reduce_fraction=D("1.0"),
            )
        )
    with pytest.raises(GMRejected, match="below one"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad2",
                action="REDUCE",
                reduce_fraction=D("1.5"),
            )
        )

    # Nonpositive fraction rejected
    with pytest.raises(GMRejected, match="positive"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad3",
                action="REDUCE",
                reduce_fraction=D("0"),
            )
        )
    with pytest.raises(GMRejected, match="positive"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad4",
                action="REDUCE",
                reduce_fraction=D("-0.5"),
            )
        )

    # Reduction too small (below min_amount) rejected
    reader.state = managed_state(
        main_quantity=D("2.0"),
        rules=VenueRules(D("0.01"), D("0.1"), D("10"), 5),  # min_amount=0.1
    )
    with pytest.raises(GMRejected, match="too small"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad5",
                action="REDUCE",
                reduce_fraction=D("0.01"),  # 2.0 * 0.01 = 0.02 < 0.1
            )
        )

    # Reduction below min_notional rejected
    reader.state = managed_state(
        main_quantity=D("2.0"),
        mark_price=D("10"),
        rules=VenueRules(D("0.01"), D("0.01"), D("50"), 5),  # min_notional=50
    )
    with pytest.raises(GMRejected, match="minimum notional"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad6",
                action="REDUCE",
                reduce_fraction=D("0.2"),  # 2.0 * 0.2 = 0.4; 0.4 * 10 = 4 < 50
            )
        )

    # Reduction that would close all rejected
    reader.state = managed_state(
        main_quantity=D("0.01"),
        rules=VenueRules(D("0.01"), D("0.01"), D("0.01"), 5),
    )
    with pytest.raises(GMRejected, match="would close all"):
        asyncio.run(
            gate.execute_management(
                correlation_id="c1",
                decision_id="d_bad7",
                action="REDUCE",
                reduce_fraction=D("0.99"),  # quantizes to 0.01 == quantity
            )
        )


def test_gm_consumer_management_pass_through(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))

    published = []
    consumer = GMConsumer(
        gm_factory=lambda sym: gate,
        publish=published.append,
    )

    # 1. HOLD -> no write, GM_MANAGEMENT_APPROVED
    hold_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c1",
        payload={"action": "HOLD"},
    )
    result = asyncio.run(consumer.handle(hold_event))
    assert result["type"] == EventType.GM_MANAGEMENT_APPROVED.value
    assert result["payload"]["result"]["action"] == "HOLD"
    assert result["payload"]["result"]["status"] == "no_write"
    assert len(published) == 1

    # 2. MANAGEMENT_BLOCKED -> explicit no-write, None returned, nothing published
    blocked_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c1",
        payload={"action": "MANAGEMENT_BLOCKED"},
    )
    assert asyncio.run(consumer.handle(blocked_event)) is None
    assert len(published) == 1

    # 3. REDUCE full decision pass-through
    reader.state = managed_state(main_quantity=D("2.0"))
    reduce_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c1",
        payload={
            "action": "REDUCE",
            "decision_id": "d_consumer_reduce",
            "reduce_fraction": "0.25",
        },
    )
    res_red = asyncio.run(consumer.handle(reduce_event))
    assert res_red["type"] == EventType.GM_MANAGEMENT_APPROVED.value
    assert port.calls[-1] == (
        "reduce",
        {"symbol": "BTC-USDT", "side": "LONG", "quantity": D("0.50"), "leverage": 2},
    )

    # 4. REDUCE GMRejected -> GM_MANAGEMENT_REJECTED
    bad_reduce_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c1",
        payload={
            "action": "REDUCE",
            "decision_id": "d_consumer_bad",
            "reduce_fraction": "1.5",
        },
    )
    res_bad = asyncio.run(consumer.handle(bad_reduce_event))
    assert res_bad["type"] == EventType.GM_MANAGEMENT_REJECTED.value
    assert res_bad["payload"]["action"] == "REDUCE"
