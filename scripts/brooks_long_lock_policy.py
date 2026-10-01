"""Pure net-exit projections for the first-operation long-lock experiment.

Open positions are closed at an adverse simulated fill: a long sells below the
mark and a short buys above it. That fill already embeds slippage, so this
module never subtracts a second slippage charge. The walk-forward simulator
does not model funding; funding is explicitly treated as zero here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypeAlias


LegSide: TypeAlias = Literal["LONG", "SHORT"]
DecimalLike: TypeAlias = Decimal | str | int

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class OpenLeg:
    """An open simulated position leg."""

    side: LegSide
    quantity: DecimalLike
    entry_price: DecimalLike


LegInput: TypeAlias = OpenLeg | Mapping[str, Any] | Sequence[Any]


def _decimal(
    value: Any,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ValueError(f"{name} must be a finite decimal value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite decimal value") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal value")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _normalize_leg(value: LegInput) -> OpenLeg:
    if isinstance(value, OpenLeg):
        side, quantity, entry_price = value.side, value.quantity, value.entry_price
    elif isinstance(value, Mapping):
        side = value.get("side")
        quantity = value.get("quantity")
        entry_price = value.get("entry_price")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 3:
            raise ValueError("leg sequence must be [side, quantity, entry_price]")
        side, quantity, entry_price = value
    else:
        raise ValueError(
            "leg must be OpenLeg, mapping, or [side, quantity, entry_price]"
        )

    if not isinstance(side, str) or side.upper() not in ("LONG", "SHORT"):
        raise ValueError("leg.side must be LONG or SHORT")
    normalized_side: LegSide = side.upper()  # type: ignore[assignment]
    normalized_quantity = _decimal(quantity, "leg.quantity", nonnegative=True)
    normalized_entry = _decimal(entry_price, "leg.entry_price", positive=True)
    return OpenLeg(normalized_side, normalized_quantity, normalized_entry)


def _normalize_inputs(
    realized_gross_pnl: DecimalLike,
    fees_paid: DecimalLike,
    legs: Sequence[LegInput],
    mark_price: DecimalLike,
    taker_fee_rate: DecimalLike,
    slippage_bps: DecimalLike,
) -> tuple[Decimal, Decimal, tuple[OpenLeg, ...], Decimal, Decimal, Decimal]:
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        raise ValueError("legs must be a sequence")
    realized = _decimal(realized_gross_pnl, "realized_gross_pnl")
    fees = _decimal(fees_paid, "fees_paid", nonnegative=True)
    normalized_legs = tuple(_normalize_leg(leg) for leg in legs)
    mark = _decimal(mark_price, "mark_price", positive=True)
    fee_rate = _decimal(taker_fee_rate, "taker_fee_rate", nonnegative=True)
    if fee_rate >= _ONE:
        raise ValueError("taker_fee_rate must be less than 1")
    slippage = _decimal(slippage_bps, "slippage_bps", nonnegative=True)
    if slippage >= _BPS:
        raise ValueError("slippage_bps must be less than 10000")
    return realized, fees, normalized_legs, mark, fee_rate, slippage


def _close_net(
    realized_gross_pnl: Decimal,
    fees_paid: Decimal,
    legs: Sequence[OpenLeg],
    mark_price: Decimal,
    taker_fee_rate: Decimal,
    slippage_bps: Decimal,
    *,
    leg_index: int | None = None,
    qty: Decimal | None = None,
) -> Decimal:
    slip_rate = slippage_bps / _BPS
    gross_on_close = _ZERO
    fees_on_close = _ZERO

    for index, leg in enumerate(legs):
        if leg_index is not None and index != leg_index:
            continue
        close_quantity = leg.quantity if qty is None else qty
        if close_quantity > leg.quantity:
            raise ValueError("close quantity exceeds selected open leg")
        if close_quantity == 0:
            continue

        if leg.side == "LONG":
            exit_fill = mark_price * (_ONE - slip_rate)
            gross_on_close += close_quantity * (exit_fill - leg.entry_price)
        else:
            exit_fill = mark_price * (_ONE + slip_rate)
            gross_on_close += close_quantity * (leg.entry_price - exit_fill)
        fees_on_close += close_quantity * exit_fill * taker_fee_rate

    # Slippage is already reflected in each adverse exit fill above. Funding is
    # absent from this simulator and therefore contributes exactly zero.
    return realized_gross_pnl + gross_on_close - fees_paid - fees_on_close


def projected_operation_exit_net(
    realized_gross_pnl: DecimalLike,
    fees_paid: DecimalLike,
    legs: Sequence[LegInput],
    mark_price: DecimalLike,
    taker_fee_rate: DecimalLike,
    slippage_bps: DecimalLike,
) -> Decimal:
    """Return net PnL if all remaining legs close at the adverse simulated fill.

    ``realized_gross_pnl`` includes prior realized fills and ``fees_paid`` includes
    fees already incurred. All open legs receive a new close fill and taker fee.
    Funding is zero under the simulator's stated assumptions.
    """
    realized, fees, normalized_legs, mark, fee_rate, slippage = _normalize_inputs(
        realized_gross_pnl,
        fees_paid,
        legs,
        mark_price,
        taker_fee_rate,
        slippage_bps,
    )
    return _close_net(realized, fees, normalized_legs, mark, fee_rate, slippage)


def net_after_leg_close(
    realized_gross_pnl: DecimalLike,
    fees_paid: DecimalLike,
    legs: Sequence[LegInput],
    mark_price: DecimalLike,
    taker_fee_rate: DecimalLike,
    slippage_bps: DecimalLike,
    *,
    leg_index: int | None = None,
    qty: DecimalLike | None = None,
) -> Decimal:
    """Return realized net PnL after closing one selected leg or all legs.

    With no ``leg_index``, the calculation closes every open leg. Selecting an
    index closes that leg fully by default or closes ``qty`` when supplied.
    Remaining open legs do not contribute unrealized PnL to this transparency
    view. ``qty`` without ``leg_index`` is rejected as ambiguous.
    """
    realized, fees, normalized_legs, mark, fee_rate, slippage = _normalize_inputs(
        realized_gross_pnl,
        fees_paid,
        legs,
        mark_price,
        taker_fee_rate,
        slippage_bps,
    )
    if leg_index is not None:
        if isinstance(leg_index, bool) or not isinstance(leg_index, int):
            raise ValueError("leg_index must be an integer")
        if not 0 <= leg_index < len(normalized_legs):
            raise ValueError("leg_index is outside the open legs")
    elif qty is not None:
        raise ValueError("qty requires leg_index")

    normalized_qty = (
        None if qty is None else _decimal(qty, "qty", nonnegative=True)
    )
    return _close_net(
        realized,
        fees,
        normalized_legs,
        mark,
        fee_rate,
        slippage,
        leg_index=leg_index,
        qty=normalized_qty,
    )


__all__ = [
    "DecimalLike",
    "LegSide",
    "OpenLeg",
    "net_after_leg_close",
    "projected_operation_exit_net",
]
