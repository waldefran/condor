"""Adversarial tests for the isolated Brooks hedge state and compiler."""

from dataclasses import replace
from decimal import Decimal

import pytest

from condor.brooks.hedge import (
    HedgeBlocked,
    PositionLeg,
    assess_hedge_result,
    build_hedge_state,
    compile_hedge_action,
)

MAIN = PositionLeg("main-1", "BTC-USDT", "LONG", "1", "100", "MAIN")
HEDGE = PositionLeg("hedge-1", "BTC-USDT", "SHORT", "0.3", "100", "HEDGE")


def state(positions=(MAIN,), *, hedge_id=None, at=100):
    return build_hedge_state(
        positions, main_position_id="main-1", hedge_position_id=hedge_id, as_of_ms=at
    )


def compile_action(action, target, before, after=None, **overrides):
    after = after or replace(before, as_of_ms=before.as_of_ms + 1)
    params = {
        "target_hedge_ratio": target,
        "plan_main_position_id": "main-1",
        "plan_hedge_position_id": before.hedge_position_id,
        "expected_state": before,
        "fresh_state": after,
        "hedge_mode_confirmed": True,
    }
    params.update(overrides)
    return compile_hedge_action(action, **params)


def test_open_increase_reduce_remove_compile_only_delta():
    initial = state()
    opened = compile_action("HEDGE", "0.3", initial)
    assert (opened.side, opened.position_action, opened.quantity) == (
        "SELL",
        "OPEN",
        "0.3",
    )
    active = state((MAIN, HEDGE), hedge_id="hedge-1")
    assert active.hedge_ratio == "0.3"
    assert (active.net_exposure_usd, active.gross_exposure_usd) == ("70.0", "130.0")
    increased = compile_action("INCREASE_HEDGE", "0.5", active)
    assert (increased.side, increased.position_action, Decimal(increased.quantity)) == (
        "SELL",
        "OPEN",
        Decimal("0.2"),
    )
    reduced = compile_action("REDUCE_HEDGE", "0.2", active)
    assert (reduced.side, reduced.position_action, Decimal(reduced.quantity)) == (
        "BUY",
        "CLOSE",
        Decimal("0.1"),
    )
    removed = compile_action("REMOVE_HEDGE", "0", active)
    assert (removed.side, removed.position_action, Decimal(removed.quantity)) == (
        "BUY",
        "CLOSE",
        Decimal("0.3"),
    )
    assert removed.main_position_id == "main-1"


def test_short_main_uses_opposite_open_and_close_sides():
    main = replace(MAIN, side="SHORT")
    hedge = replace(HEDGE, side="LONG")
    single = state((main,))
    active = state((main, hedge), hedge_id="hedge-1")
    assert compile_action("HEDGE", "0.3", single).side == "BUY"
    assert compile_action("REMOVE_HEDGE", "0", active).side == "SELL"
    assert active.net_exposure_usd == "-70.0"


def test_absolute_mark_notional_uses_each_legs_mark():
    active = state(
        (MAIN, replace(HEDGE, quantity="0.25", mark_price="120")), hedge_id="hedge-1"
    )
    assert active.hedge_ratio == "0.30"
    command = compile_action("INCREASE_HEDGE", "0.6", active)
    assert Decimal(command.quantity) == Decimal("0.25")


def test_quantity_increment_and_minimum_block_unexecutable_deltas():
    initial = state()
    command = compile_action("HEDGE", "0.333", initial, quantity_increment="0.01")
    assert command.quantity == "0.33"
    assert command.expected_hedge_size == "0.33"
    with pytest.raises(HedgeBlocked, match="minimum"):
        compile_action("HEDGE", "0.001", initial, minimum_quantity="0.01")
    active = state((MAIN, replace(HEDGE, quantity="0.305")), hedge_id="hedge-1")
    with pytest.raises(HedgeBlocked, match="increment"):
        compile_action("REMOVE_HEDGE", "0", active, quantity_increment="0.01")


@pytest.mark.parametrize(
    ("positions", "hedge_id", "status"),
    [
        ((MAIN, replace(MAIN, position_id="main-2")), None, "duplicate_main"),
        (
            (MAIN, HEDGE, replace(HEDGE, position_id="hedge-2")),
            "hedge-1",
            "duplicate_hedge",
        ),
        ((HEDGE,), "hedge-1", "orphan_hedge"),
        ((MAIN, replace(HEDGE, ownership_role=None)), None, "unknown_role"),
        ((MAIN, replace(HEDGE, side="LONG")), "hedge-1", "inconsistent_ownership"),
        ((MAIN, HEDGE), None, "inconsistent_ownership"),
        ((MAIN,), "hedge-1", "inconsistent_ownership"),
        ((), None, "no_positions"),
        ((MAIN, MAIN), None, "duplicate_main"),
    ],
)
def test_ambiguous_structures_fail_closed(positions, hedge_id, status):
    observed = state(positions, hedge_id=hedge_id)
    assert observed.structure_status == status
    assert observed.unresolved
    with pytest.raises(HedgeBlocked):
        compile_action("HEDGE", "0.2", observed)


@pytest.mark.parametrize("target", ["-0.1", "1.1", "01", "0.", "NaN", "0e0", 0.2])
def test_invalid_target_ratio_rejected(target):
    with pytest.raises(HedgeBlocked):
        compile_action("HEDGE", target, state())


@pytest.mark.parametrize(
    ("action", "target", "active"),
    [
        ("REMOVE_HEDGE", "0.1", True),
        ("INCREASE_HEDGE", "0.3", True),
        ("INCREASE_HEDGE", "0.2", True),
        ("REDUCE_HEDGE", "0.3", True),
        ("REDUCE_HEDGE", "0.4", True),
        ("REDUCE_HEDGE", "0", True),
        ("HEDGE", "0.5", True),
        ("HEDGE", "0", False),
    ],
)
def test_invalid_action_transitions_rejected(action, target, active):
    observed = state((MAIN, HEDGE), hedge_id="hedge-1") if active else state()
    with pytest.raises(HedgeBlocked):
        compile_action(action, target, observed)


def test_stale_disappeared_or_pending_snapshot_blocks_write():
    single = state()
    with pytest.raises(HedgeBlocked, match="stale"):
        compile_action("HEDGE", "0.2", single, single)
    disappeared = state((), at=101)
    with pytest.raises(HedgeBlocked, match="stale"):
        compile_action("HEDGE", "0.2", single, disappeared)
    changed = state((replace(MAIN, quantity="0.9"),), at=101)
    with pytest.raises(HedgeBlocked, match="stale"):
        compile_action("HEDGE", "0.2", single, changed)
    with pytest.raises(HedgeBlocked):
        compile_action("HEDGE", "0.2", single, pending_order=True)
    with pytest.raises(HedgeBlocked):
        compile_action("HEDGE", "0.2", single, hedge_mode_confirmed=False)


def test_plan_ids_and_binding_cannot_be_guessed_from_side_or_size():
    active = state((MAIN, HEDGE), hedge_id="hedge-1")
    with pytest.raises(HedgeBlocked):
        compile_action("REMOVE_HEDGE", "0", active, plan_main_position_id="hedge-1")
    with pytest.raises(HedgeBlocked):
        compile_action("REMOVE_HEDGE", "0", active, plan_hedge_position_id="other")


def test_partial_failure_ambiguous_and_full_fill_require_reconciliation():
    initial = state()
    command = compile_action("HEDGE", "0.3", initial)
    half = state((MAIN, replace(HEDGE, quantity="0.1")), hedge_id="hedge-1", at=102)
    full = state((MAIN, HEDGE), hedge_id="hedge-1", at=102)
    assert (
        assess_hedge_result(
            command, outcome="succeeded", filled_quantity="0.1", reconciled_state=half
        ).status
        == "partial"
    )
    assert (
        assess_hedge_result(
            command,
            outcome="failed",
            filled_quantity="0",
            reconciled_state=state(at=102),
        ).status
        == "failed"
    )
    assert (
        assess_hedge_result(
            command, outcome="failed", filled_quantity="0", reconciled_state=None
        ).status
        == "ambiguous"
    )
    assert (
        assess_hedge_result(
            command, outcome="failed", filled_quantity="0.1", reconciled_state=half
        ).status
        == "ambiguous"
    )
    assert (
        assess_hedge_result(
            command, outcome="unknown", filled_quantity="0", reconciled_state=None
        ).status
        == "ambiguous"
    )
    assert (
        assess_hedge_result(
            command, outcome="succeeded", filled_quantity="0.3", reconciled_state=None
        ).status
        == "ambiguous"
    )
    assert (
        assess_hedge_result(
            command, outcome="succeeded", filled_quantity="0.3", reconciled_state=full
        ).status
        == "confirmed"
    )


def test_restart_rebuilds_explicit_open_main_and_hedge():
    persisted_main_id, persisted_hedge_id = "main-1", "hedge-1"
    restarted = build_hedge_state(
        (HEDGE, MAIN),
        main_position_id=persisted_main_id,
        hedge_position_id=persisted_hedge_id,
        as_of_ms=200,
    )
    assert restarted.structure_status == "ok"
    assert (
        compile_action("REMOVE_HEDGE", "0", restarted).hedge_position_id
        == persisted_hedge_id
    )
