"""Pure, fail-closed Brooks hedge ownership and order compilation.

The caller owns the account/connector/symbol lock, obtains two independent
venue reads, confirms HEDGE position mode, and submits the resulting command.
This module never calls an exchange or assigns ownership from a position side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal, Sequence

Side = Literal["LONG", "SHORT"]
Role = Literal["MAIN", "HEDGE", "PROTECTIVE", "UNRESOLVED"]
HedgeAction = Literal["HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"]
StructureStatus = Literal[
    "ok",
    "single_main",
    "orphan_hedge",
    "unknown_role",
    "duplicate_main",
    "duplicate_hedge",
    "inconsistent_ownership",
    "no_positions",
]


class HedgeBlocked(ValueError):
    """A hedge mutation needs reconciliation or a new authoritative snapshot."""


def _decimal(value: str | Decimal, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise HedgeBlocked(f"{name} must be a decimal string")
    if isinstance(value, str) and (not value or value.strip() != value):
        raise HedgeBlocked(f"{name} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise HedgeBlocked(f"{name} is not a decimal") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise HedgeBlocked(
            f"{name} must be finite and {'positive' if positive else 'nonnegative'}"
        )
    return number


def _ratio(value: str, name: str) -> Decimal:
    if not isinstance(value, str) or not value or not value.isascii():
        raise HedgeBlocked(f"{name} must be a canonical decimal string")
    whole, dot, fraction = value.partition(".")
    if (
        not whole.isdecimal()
        or (len(whole) > 1 and whole[0] == "0")
        or (dot and not fraction.isdecimal())
    ):
        raise HedgeBlocked(f"{name} must be a canonical decimal string")
    result = _decimal(value, name)
    if result > 1:
        raise HedgeBlocked(f"{name} must be within [0, 1]")
    return result


def _plain(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class PositionLeg:
    position_id: str
    symbol: str
    side: Side
    quantity: str
    mark_price: str
    ownership_role: Role | None


@dataclass(frozen=True, slots=True)
class HedgeState:
    schema: str
    structure_status: StructureStatus
    unresolved: bool
    main_position_id: str | None
    hedge_position_id: str | None
    symbol: str | None
    main_side: Side | None
    hedge_side: Side | None
    main_size: str
    hedge_size: str
    hedge_ratio: str
    ratio_basis: str
    net_exposure_usd: str
    gross_exposure_usd: str
    main_mark_notional: str
    hedge_mark_notional: str
    mark_price: str | None
    hedge_mark_price: str | None
    as_of_ms: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class HedgeCommand:
    action: HedgeAction
    main_position_id: str
    hedge_position_id: str | None
    symbol: str
    side: Literal["BUY", "SELL"]
    position_action: Literal["OPEN", "CLOSE"]
    quantity: str
    target_hedge_ratio: str
    expected_hedge_size: str
    state_fingerprint: str
    state_as_of_ms: int


@dataclass(frozen=True, slots=True)
class HedgeResult:
    status: Literal["confirmed", "partial", "failed", "ambiguous"]
    reason: str


def build_hedge_state(
    positions: Sequence[PositionLeg],
    *,
    main_position_id: str | None,
    hedge_position_id: str | None,
    as_of_ms: int,
) -> HedgeState:
    """Reconcile venue legs against persisted, explicit role-to-id binding."""
    if not isinstance(as_of_ms, int) or isinstance(as_of_ms, bool) or as_of_ms < 0:
        raise HedgeBlocked("as_of_ms must be a nonnegative integer")
    if main_position_id is not None and not main_position_id:
        raise HedgeBlocked("main_position_id cannot be empty")
    if hedge_position_id is not None and not hedge_position_id:
        raise HedgeBlocked("hedge_position_id cannot be empty")

    ids: set[str] = set()
    mains: list[PositionLeg] = []
    hedges: list[PositionLeg] = []
    unknown = False
    duplicate_id = False
    invalid = False
    symbols: set[str] = set()
    for leg in positions:
        if not leg.position_id or not leg.symbol or leg.side not in ("LONG", "SHORT"):
            invalid = True
        if leg.position_id in ids:
            duplicate_id = True
        ids.add(leg.position_id)
        symbols.add(leg.symbol)
        try:
            _decimal(leg.quantity, "quantity", positive=True)
            _decimal(leg.mark_price, "mark_price", positive=True)
        except HedgeBlocked:
            invalid = True
        if leg.ownership_role == "MAIN":
            mains.append(leg)
        elif leg.ownership_role == "HEDGE":
            hedges.append(leg)
        elif leg.ownership_role != "PROTECTIVE":
            unknown = True

    if len(mains) > 1:
        status: StructureStatus = "duplicate_main"
    elif len(hedges) > 1:
        status = "duplicate_hedge"
    elif not mains and hedges:
        status = "orphan_hedge"
    elif unknown:
        status = "unknown_role"
    elif not positions:
        status = "no_positions"
    elif (
        invalid
        or duplicate_id
        or len(symbols) > 1
        or main_position_id == hedge_position_id
        and main_position_id is not None
    ):
        status = "inconsistent_ownership"
    else:
        main = mains[0] if mains else None
        hedge = hedges[0] if hedges else None
        if (
            main is None
            or main.position_id != main_position_id
            or (hedge.position_id if hedge else None) != hedge_position_id
            or (hedge is not None and hedge.side == main.side)
        ):
            status = "inconsistent_ownership"
        else:
            status = "ok" if hedge else "single_main"

    main = mains[0] if len(mains) == 1 else None
    hedge = hedges[0] if len(hedges) == 1 else None
    main_qty = (
        _decimal(main.quantity, "main quantity")
        if main and status in ("ok", "single_main")
        else Decimal(0)
    )
    hedge_qty = (
        _decimal(hedge.quantity, "hedge quantity")
        if hedge and status == "ok"
        else Decimal(0)
    )
    main_notional = (
        main_qty * _decimal(main.mark_price, "main mark") if main_qty else Decimal(0)
    )
    hedge_notional = (
        hedge_qty * _decimal(hedge.mark_price, "hedge mark")
        if hedge_qty
        else Decimal(0)
    )
    if status == "ok" and hedge_notional > main_notional:
        status = "inconsistent_ownership"
    ratio = hedge_notional / main_notional if main_notional else Decimal(0)
    direction = Decimal(1) if main and main.side == "LONG" else Decimal(-1)
    # Structure-only: marks move every read on a live venue and must not trip
    # staleness by themselves; quantity/side/id/role changes still do.
    fingerprint_data = sorted(
        (
            p.position_id,
            p.symbol,
            p.side,
            str(p.quantity),
            str(p.ownership_role),
        )
        for p in positions
    )
    fingerprint = sha256(
        repr((main_position_id, hedge_position_id, fingerprint_data)).encode()
    ).hexdigest()
    return HedgeState(
        schema="condor.brooks.hedge-state.v1",
        structure_status=status,
        unresolved=status not in ("ok", "single_main"),
        main_position_id=main_position_id,
        hedge_position_id=hedge_position_id,
        symbol=main.symbol if main and status in ("ok", "single_main") else None,
        main_side=main.side if main else None,
        hedge_side=hedge.side if hedge else None,
        main_size=_plain(main_qty),
        hedge_size=_plain(hedge_qty),
        hedge_ratio=_plain(ratio),
        ratio_basis="absolute_mark_notional",
        net_exposure_usd=_plain(direction * (main_notional - hedge_notional)),
        gross_exposure_usd=_plain(main_notional + hedge_notional),
        main_mark_notional=_plain(main_notional),
        hedge_mark_notional=_plain(hedge_notional),
        mark_price=(
            main.mark_price if main and status in ("ok", "single_main") else None
        ),
        hedge_mark_price=hedge.mark_price if hedge and status == "ok" else None,
        as_of_ms=as_of_ms,
        fingerprint=fingerprint,
    )


def compile_hedge_action(
    action: HedgeAction,
    *,
    target_hedge_ratio: str,
    plan_main_position_id: str,
    plan_hedge_position_id: str | None,
    expected_state: HedgeState,
    fresh_state: HedgeState,
    hedge_mode_confirmed: bool,
    pending_order: bool = False,
    quantity_increment: str | None = None,
    minimum_quantity: str | None = None,
) -> HedgeCommand:
    """Compile one delta after a locked, newer venue read confirms the decision state."""
    if action not in ("HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"):
        raise HedgeBlocked("unsupported hedge action")
    target = _ratio(target_hedge_ratio, "target_hedge_ratio")
    if not hedge_mode_confirmed or pending_order:
        raise HedgeBlocked("hedge mode must be confirmed and prior orders settled")
    if (
        fresh_state.as_of_ms <= expected_state.as_of_ms
        or fresh_state.fingerprint != expected_state.fingerprint
    ):
        raise HedgeBlocked("stale position snapshot; reconcile before writing")
    required_status = "single_main" if action == "HEDGE" else "ok"
    if fresh_state.unresolved or fresh_state.structure_status != required_status:
        raise HedgeBlocked(f"{action} requires {required_status} ownership")
    if fresh_state.ratio_basis != "absolute_mark_notional":
        raise HedgeBlocked("unsupported ratio basis")
    if fresh_state.main_side not in ("LONG", "SHORT") or (
        fresh_state.hedge_side is not None
        and fresh_state.hedge_side == fresh_state.main_side
    ):
        raise HedgeBlocked("inconsistent MAIN/HEDGE sides")
    if (
        not plan_main_position_id
        or plan_main_position_id != fresh_state.main_position_id
        or plan_hedge_position_id != fresh_state.hedge_position_id
    ):
        raise HedgeBlocked(
            "hedge plan position IDs do not match authoritative ownership"
        )
    current = _ratio(fresh_state.hedge_ratio, "hedge_ratio")
    main_notional = _decimal(
        fresh_state.main_mark_notional, "main_mark_notional", positive=True
    )
    hedge_notional = _decimal(fresh_state.hedge_mark_notional, "hedge_mark_notional")
    mark = _decimal(
        fresh_state.hedge_mark_price or fresh_state.mark_price or "",
        "mark_price",
        positive=True,
    )
    hedge_qty = _decimal(fresh_state.hedge_size, "hedge_size")
    if action == "HEDGE" and (
        current != 0 or target <= 0 or fresh_state.hedge_position_id is not None
    ):
        raise HedgeBlocked("HEDGE requires an absent hedge and a positive target")
    if action == "INCREASE_HEDGE" and target <= current:
        raise HedgeBlocked("INCREASE_HEDGE target must exceed current ratio")
    if action == "REDUCE_HEDGE" and not (0 < target < current):
        raise HedgeBlocked("REDUCE_HEDGE target must be between zero and current ratio")
    if action == "REMOVE_HEDGE" and target != 0:
        raise HedgeBlocked("REMOVE_HEDGE target must be zero")
    desired_notional = main_notional * target
    delta_notional = desired_notional - hedge_notional
    if action in ("HEDGE", "INCREASE_HEDGE") and delta_notional <= 0:
        raise HedgeBlocked("target produces no OPEN delta")
    if action in ("REDUCE_HEDGE", "REMOVE_HEDGE") and delta_notional >= 0:
        raise HedgeBlocked("target produces no CLOSE delta")
    quantity = abs(delta_notional) / mark
    if action == "REMOVE_HEDGE":
        quantity = hedge_qty  # close the observed leg exactly, including partial fills
    if quantity_increment is not None:
        increment = _decimal(quantity_increment, "quantity_increment", positive=True)
        if action == "REMOVE_HEDGE" and quantity % increment:
            raise HedgeBlocked("full hedge size is not aligned to quantity increment")
        quantity = (quantity // increment) * increment
    if minimum_quantity is not None and quantity < _decimal(
        minimum_quantity, "minimum_quantity", positive=True
    ):
        raise HedgeBlocked("hedge delta is below minimum quantity")
    if action == "REDUCE_HEDGE" and quantity >= hedge_qty:
        raise HedgeBlocked("REDUCE_HEDGE would close the entire hedge")
    if quantity <= 0:
        raise HedgeBlocked("zero hedge quantity")
    opening = action in ("HEDGE", "INCREASE_HEDGE")
    hedge_side: Side = "SHORT" if fresh_state.main_side == "LONG" else "LONG"
    order_side: Literal["BUY", "SELL"] = (
        ("SELL" if hedge_side == "SHORT" else "BUY")
        if opening
        else ("BUY" if hedge_side == "SHORT" else "SELL")
    )
    return HedgeCommand(
        action=action,
        main_position_id=plan_main_position_id,
        hedge_position_id=plan_hedge_position_id,
        symbol=_state_symbol(fresh_state),
        side=order_side,
        position_action="OPEN" if opening else "CLOSE",
        quantity=_plain(quantity),
        target_hedge_ratio=target_hedge_ratio,
        expected_hedge_size=_plain(hedge_qty + (quantity if opening else -quantity)),
        state_fingerprint=fresh_state.fingerprint,
        state_as_of_ms=fresh_state.as_of_ms,
    )


def _state_symbol(state: HedgeState) -> str:
    symbol = state.symbol
    if not isinstance(symbol, str) or not symbol:
        raise HedgeBlocked("hedge state lacks authoritative symbol")
    return symbol


def assess_hedge_result(
    command: HedgeCommand,
    *,
    outcome: Literal["succeeded", "failed", "unknown"],
    filled_quantity: str,
    reconciled_state: HedgeState | None,
) -> HedgeResult:
    """Classify a write without treating an ack or partial fill as completion."""
    filled = _decimal(filled_quantity, "filled_quantity")
    requested = _decimal(command.quantity, "command quantity", positive=True)
    if (
        filled > requested
        or outcome == "unknown"
        or (outcome == "failed" and filled > 0)
    ):
        return HedgeResult("ambiguous", "execution needs reconciliation")
    if outcome == "failed":
        if (
            reconciled_state is None
            or reconciled_state.as_of_ms <= command.state_as_of_ms
            or reconciled_state.fingerprint != command.state_fingerprint
        ):
            return HedgeResult(
                "ambiguous", "failed order has no unchanged venue confirmation"
            )
        return HedgeResult("failed", "order failed and venue state is unchanged")
    if reconciled_state is None or reconciled_state.as_of_ms <= command.state_as_of_ms:
        return HedgeResult("ambiguous", "no newer authoritative position state")
    if (
        reconciled_state.unresolved
        or reconciled_state.main_position_id != command.main_position_id
    ):
        return HedgeResult("ambiguous", "post-write ownership is unresolved")
    if (
        command.hedge_position_id is not None
        and reconciled_state.hedge_position_id not in (command.hedge_position_id, None)
    ):
        return HedgeResult("ambiguous", "post-write HEDGE identity changed")
    if filled == 0:
        return HedgeResult("ambiguous", "success reported without a fill")
    expected = _decimal(command.expected_hedge_size, "expected hedge size")
    remaining = requested - filled
    actual_expected = (
        expected - remaining
        if command.position_action == "OPEN"
        else expected + remaining
    )
    if (
        _decimal(reconciled_state.hedge_size, "reconciled hedge size")
        != actual_expected
    ):
        return HedgeResult("ambiguous", "fill and reconciled quantity disagree")
    if filled < requested:
        return HedgeResult(
            "partial", "partial fill; re-read and reauthorize before any retry"
        )
    return HedgeResult("confirmed", "filled quantity matches reconciled hedge state")
