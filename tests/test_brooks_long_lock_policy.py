"""Economic checks for the PM long-lock exit policy projections."""

from decimal import Decimal

import pytest

from scripts.brooks_long_lock_policy import (
    OpenLeg,
    net_after_leg_close,
    projected_operation_exit_net,
)


def test_projected_exit_uses_one_adverse_fill_and_charges_close_fee():
    net = projected_operation_exit_net(
        realized_gross_pnl="-2",
        fees_paid="1",
        legs=[OpenLeg("LONG", Decimal("2"), "100")],
        mark_price="105",
        taker_fee_rate="0.01",
        slippage_bps="100",
    )

    # Long exits at 105 * .99 = 103.95. The 7.90 gross exit PnL already
    # includes slippage; only the 2.079 close fee is subtracted separately.
    assert net == Decimal("2.821000")


def test_opposing_legs_lock_gross_pnl_across_marks():
    legs = [("LONG", "1", "100"), ("SHORT", "1", "110")]
    at_100 = projected_operation_exit_net("-3", "1", legs, "100", "0", "0")
    at_130 = projected_operation_exit_net("-3", "1", legs, "130", "0", "0")

    # The open legs' combined gross PnL is +10 at either mark. Existing gross
    # loss and fees reduce it to +6 net in both cases.
    assert at_100 == at_130 == Decimal("6")


def test_close_fees_include_both_open_legs():
    net = projected_operation_exit_net(
        realized_gross_pnl="0",
        fees_paid="0.25",
        legs=[("LONG", "1", "100"), ("SHORT", "2", "100")],
        mark_price="100",
        taker_fee_rate="0.01",
        slippage_bps="0",
    )

    assert net == Decimal("-3.25")


def test_zero_open_legs_cannot_create_mark_to_market_profit():
    net = projected_operation_exit_net(
        realized_gross_pnl="-4.25",
        fees_paid="1.25",
        legs=[],
        mark_price="1000000",
        taker_fee_rate="0.01",
        slippage_bps="50",
    )
    zero_quantity_leg = projected_operation_exit_net(
        realized_gross_pnl="-4.25",
        fees_paid="1.25",
        legs=[("LONG", "0", "100")],
        mark_price="1000000",
        taker_fee_rate="0.01",
        slippage_bps="50",
    )

    assert net == zero_quantity_leg == Decimal("-5.50")


def test_partial_leg_close_reports_only_realized_result_for_that_close():
    net = net_after_leg_close(
        realized_gross_pnl="-3",
        fees_paid="1",
        legs=[("LONG", "2", "100"), ("SHORT", "1", "110")],
        mark_price="110",
        taker_fee_rate="0.01",
        slippage_bps="0",
        leg_index=0,
        qty="1",
    )

    # One long closes for +10 gross and a 1.10 fee. The still-open short is
    # excluded from this realized-only transparency value.
    assert net == Decimal("4.90")


@pytest.mark.parametrize(
    ("kwargs", "legs"),
    [
        ({"realized_gross_pnl": "NaN"}, []),
        ({"fees_paid": "-1"}, []),
        ({}, [("BOTH", "1", "100")]),
        ({}, [("LONG", "-1", "100")]),
        ({}, [("SHORT", "1", "0")]),
        ({"mark_price": "0"}, []),
        ({"taker_fee_rate": "Infinity"}, []),
        ({"slippage_bps": "10000"}, []),
    ],
)
def test_rejects_invalid_decimal_and_leg_inputs(kwargs, legs):
    arguments = {
        "realized_gross_pnl": "0",
        "fees_paid": "0",
        "legs": legs,
        "mark_price": "100",
        "taker_fee_rate": "0.001",
        "slippage_bps": "1",
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        projected_operation_exit_net(**arguments)


def test_partial_close_rejects_ambiguous_or_excess_quantity():
    common = {
        "realized_gross_pnl": "0",
        "fees_paid": "0",
        "legs": [("LONG", "1", "100")],
        "mark_price": "101",
        "taker_fee_rate": "0",
        "slippage_bps": "0",
    }
    with pytest.raises(ValueError, match="requires leg_index"):
        net_after_leg_close(**common, qty="0.5")
    with pytest.raises(ValueError, match="exceeds"):
        net_after_leg_close(**common, leg_index=0, qty="1.1")
