"""Close-confirmed activation for simulated pending Brooks stop entries.

The helper is deliberately pure: it never writes an order or changes the
original Trader record. It turns a pending stop into a derived execution
intent only after an observed, closed M1 candle crossed the stop and closed
beyond it. The MARKET execution therefore uses a known candle close rather
than a retrospective intrabar fill.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from condor.brooks.contracts import TradeIntentV2


_M1_MS = 60_000
_M1_CLOSE_OFFSET_MS = _M1_MS - 1
_DEFAULT_MAX_INTENT_AGE_MS = 7_200_000


class PendingEntryError(ValueError):
    """An intent, replay clock, candle, or checkpoint is invalid."""


@dataclass(frozen=True)
class M1Bar:
    """A closed one-minute candle with exact decimal prices."""

    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    closed: bool = True


class PendingStopState(BaseModel):
    """JSON-safe checkpoint returned after processing pending-stop candles."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    state_schema: Literal["condor.brooks.pending-entry-state.v1"] = Field(
        default="condor.brooks.pending-entry-state.v1",
        alias="schema",
        serialization_alias="schema",
    )
    correlation_id: StrictStr
    symbol: StrictStr
    decision_time_ms: int
    available_at_ms: int
    expires_at_ms: int
    max_intent_age_ms: int
    status: Literal["pending", "triggered", "canceled", "expired"] = "pending"
    last_processed_close_time_ms: int | None = None
    changed_at_ms: int | None = None
    resolution_reason: StrictStr | None = None
    evidence: dict[str, Any] | None = None
    execution_intent: dict[str, Any] | None = None

    def model_dump(self, *, mode: str = "python", **kwargs: Any) -> dict[str, Any]:
        """Return a checkpoint; JSON mode contains only primitive JSON values."""
        kwargs.setdefault("by_alias", True)
        return super().model_dump(mode=mode, **kwargs)


def _strict_timestamp(value: Any, name: str, *, nonnegative: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PendingEntryError(f"{name} must be an integer timestamp")
    if nonnegative and value < 0:
        raise PendingEntryError(f"{name} must be nonnegative")
    return value


def _positive_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise PendingEntryError(f"{name} must be a finite decimal value")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PendingEntryError(f"{name} must be a finite decimal value") from None
    if not number.is_finite() or number <= 0:
        raise PendingEntryError(f"{name} must be a positive finite decimal value")
    return number


def _intent_data(intent: TradeIntentV2 | Mapping[str, Any]) -> dict[str, Any]:
    try:
        if isinstance(intent, TradeIntentV2):
            validated = intent
        elif isinstance(intent, Mapping):
            validated = TradeIntentV2.model_validate(dict(intent))
        else:
            raise PendingEntryError("intent must be a validated TradeIntentV2")
    except ValidationError as exc:
        raise PendingEntryError("intent is not a valid TradeIntentV2") from exc
    return validated.model_dump(mode="json")


def _normalize_bar(value: M1Bar | Mapping[str, Any]) -> M1Bar:
    if isinstance(value, M1Bar):
        raw: Mapping[str, Any] = {
            "open_time_ms": value.open_time_ms,
            "close_time_ms": value.close_time_ms,
            "open": value.open,
            "high": value.high,
            "low": value.low,
            "close": value.close,
            "closed": value.closed,
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise PendingEntryError("each candle must be an M1Bar or mapping")

    opened = _strict_timestamp(raw.get("open_time_ms"), "bar.open_time_ms")
    closed_at = _strict_timestamp(raw.get("close_time_ms"), "bar.close_time_ms")
    if opened % _M1_MS != 0 or closed_at != opened + _M1_CLOSE_OFFSET_MS:
        raise PendingEntryError("bar must be an exact, closed one-minute candle")
    if "closed" in raw and raw["closed"] is not True:
        raise PendingEntryError("bar.closed must be true")

    prices = {
        name: _positive_decimal(raw.get(name), f"bar.{name}")
        for name in ("open", "high", "low", "close")
    }
    if (
        prices["high"] < max(prices["open"], prices["close"], prices["low"])
        or prices["low"] > min(prices["open"], prices["close"], prices["high"])
    ):
        raise PendingEntryError("bar OHLC values are inconsistent")
    return M1Bar(
        opened,
        closed_at,
        prices["open"],
        prices["high"],
        prices["low"],
        prices["close"],
    )


def _bar_data(bar: M1Bar) -> dict[str, Any]:
    return {
        "timeframe": "M1",
        "open_time_ms": bar.open_time_ms,
        "close_time_ms": bar.close_time_ms,
        "open": format(bar.open, "f"),
        "high": format(bar.high, "f"),
        "low": format(bar.low, "f"),
        "close": format(bar.close, "f"),
    }


def _safe_correlation_id(value: Any) -> str:
    if not isinstance(value, str) or not value or not all(
        char.isalnum() or char in "-_" for char in value
    ):
        raise PendingEntryError("correlation_id is invalid")
    return value


def _load_state(
    state: PendingStopState | Mapping[str, Any] | None,
) -> PendingStopState | None:
    if state is None:
        return None
    if isinstance(state, PendingStopState):
        return state
    if not isinstance(state, Mapping):
        raise PendingEntryError("state must be a PendingStopState or JSON mapping")
    try:
        return PendingStopState.model_validate(dict(state))
    except ValidationError as exc:
        raise PendingEntryError("pending entry checkpoint is invalid") from exc


def _derived_execution_intent(original: dict[str, Any]) -> dict[str, Any]:
    # model_dump creates a fresh primitive tree, so the original model or dict
    # remains untouched even when its setup is changed in this derivative.
    derived = dict(original)
    original_setup = original.get("setup")
    setup = dict(original_setup) if isinstance(original_setup, dict) else {}
    setup["trigger_status"] = "triggered"
    derived["setup"] = setup
    try:
        return TradeIntentV2.model_validate(derived).model_dump(mode="json")
    except ValidationError as exc:  # defensive; a validated pending intent stays valid
        raise PendingEntryError(
            "could not derive a triggered execution intent"
        ) from exc


def process_pending_stop(
    intent: TradeIntentV2 | Mapping[str, Any],
    *,
    correlation_id: str,
    available_at_ms: int,
    closed_bars: Sequence[M1Bar | Mapping[str, Any]],
    as_of_ms: int,
    max_intent_age_ms: int = _DEFAULT_MAX_INTENT_AGE_MS,
    state: PendingStopState | Mapping[str, Any] | None = None,
) -> PendingStopState:
    """Advance one pending stop over observed, closed M1 bars.

    ``available_at_ms`` is the simulated Trader completion time. A candle
    whose open precedes that time is excluded, even if its close follows it,
    because that candle was already forming when the intent became available.
    ``as_of_ms`` is the caller's replay clock and rejects future candles.

    Checkpoints make repeated delivery idempotent: bars at or before the saved
    cursor are skipped, and a triggered/canceled/expired state is terminal.
    Expiry is inclusive at ``decision_time_ms + max_intent_age_ms``; only bars
    fully closed by that instant can resolve the pending entry.
    """
    original = _intent_data(intent)
    correlation_id = _safe_correlation_id(correlation_id)
    available = _strict_timestamp(available_at_ms, "available_at_ms")
    as_of = _strict_timestamp(as_of_ms, "as_of_ms")
    if (
        isinstance(max_intent_age_ms, bool)
        or not isinstance(max_intent_age_ms, int)
        or max_intent_age_ms <= 0
    ):
        raise PendingEntryError("max_intent_age_ms must be a positive integer")
    if as_of < available:
        raise PendingEntryError("as_of_ms precedes available_at_ms")

    decision_time = original["decision_time_ms"]
    if available < decision_time:
        raise PendingEntryError("available_at_ms precedes the Trader decision time")
    if original["decision"] not in ("ENTER_LONG", "ENTER_SHORT"):
        raise PendingEntryError("pending stop intent must be an entry")
    setup = original.get("setup")
    trigger = original.get("trigger")
    invalidation = original.get("invalidation")
    if (
        not isinstance(setup, dict)
        or setup.get("trigger_status") != "pending"
        or not isinstance(trigger, dict)
        or trigger.get("kind") != "stop"
        or not isinstance(invalidation, dict)
    ):
        raise PendingEntryError("intent must contain a pending stop and invalidation")

    long_side = original["decision"] == "ENTER_LONG"
    expected_direction = "above" if long_side else "below"
    if trigger.get("direction") != expected_direction:
        raise PendingEntryError("pending stop direction does not match the entry side")
    trigger_price = _positive_decimal(trigger.get("price"), "trigger.price")
    invalidation_price = _positive_decimal(
        invalidation.get("price"), "invalidation.price"
    )
    if (long_side and invalidation_price >= trigger_price) or (
        not long_side and invalidation_price <= trigger_price
    ):
        raise PendingEntryError("invalidation is on the wrong side of the pending stop")

    expires_at = decision_time + max_intent_age_ms
    loaded_state = _load_state(state)
    if loaded_state is None:
        current = PendingStopState(
            correlation_id=correlation_id,
            symbol=original["symbol"],
            decision_time_ms=decision_time,
            available_at_ms=available,
            expires_at_ms=expires_at,
            max_intent_age_ms=max_intent_age_ms,
        )
    else:
        current = loaded_state
        if (
            current.correlation_id != correlation_id
            or current.symbol != original["symbol"]
            or current.decision_time_ms != decision_time
            or current.available_at_ms != available
            or current.expires_at_ms != expires_at
            or current.max_intent_age_ms != max_intent_age_ms
        ):
            raise PendingEntryError("checkpoint does not match this pending intent")

    if not isinstance(closed_bars, Sequence) or isinstance(closed_bars, (str, bytes)):
        raise PendingEntryError("closed_bars must be a sequence")
    bars = [_normalize_bar(raw) for raw in closed_bars]
    for bar in bars:
        if bar.close_time_ms > as_of:
            raise PendingEntryError("bar closes after as_of_ms")
    if current.status != "pending":
        return current

    cursor = current.last_processed_close_time_ms
    last_input_open: int | None = None
    for bar in bars:
        if last_input_open is not None and bar.open_time_ms < last_input_open:
            # Repeated historical input is okay when it is already behind the
            # saved cursor; genuinely new out-of-order bars are not.
            if cursor is None or bar.close_time_ms > cursor:
                raise PendingEntryError("closed M1 bars must be sequential")
        last_input_open = bar.open_time_ms
        if bar.open_time_ms < available:
            continue
        if cursor is not None and bar.close_time_ms <= cursor:
            continue
        if cursor is not None and bar.open_time_ms <= cursor:
            raise PendingEntryError("new M1 bar overlaps the processed cursor")
        if bar.close_time_ms > expires_at:
            break

        invalidation_hit = (
            bar.low <= invalidation_price
            if long_side
            else bar.high >= invalidation_price
        )
        trigger_crossed = (
            bar.high > trigger_price if long_side else bar.low < trigger_price
        )
        close_beyond_trigger = (
            bar.close > trigger_price if long_side else bar.close < trigger_price
        )
        close_protective = (
            bar.close > invalidation_price
            if long_side
            else bar.close < invalidation_price
        )
        base = current.model_dump(mode="python")
        base["last_processed_close_time_ms"] = bar.close_time_ms

        if invalidation_hit:
            base.update(
                status="canceled",
                changed_at_ms=bar.close_time_ms,
                resolution_reason="invalidation_touched_before_activation",
                evidence={
                    "event": "canceled",
                    "changed_at_ms": bar.close_time_ms,
                    "reason": "invalidation_touched_before_activation",
                    "invalidation_price": format(invalidation_price, "f"),
                    "invalidation_hit": True,
                    "trigger_crossed_same_bar": trigger_crossed,
                    "bar": _bar_data(bar),
                },
            )
            return PendingStopState.model_validate(base)

        if trigger_crossed and close_beyond_trigger and close_protective:
            execution_intent = _derived_execution_intent(original)
            base.update(
                status="triggered",
                changed_at_ms=bar.close_time_ms,
                resolution_reason="close_confirmed_stop_trigger",
                evidence={
                    "event": "activated",
                    "changed_at_ms": bar.close_time_ms,
                    "reason": "close_confirmed_stop_trigger",
                    "trigger_price": format(trigger_price, "f"),
                    "invalidation_price": format(invalidation_price, "f"),
                    "trigger_crossed": True,
                    "close_beyond_trigger": True,
                    "close_protective": True,
                    "execution_reference_price": format(bar.close, "f"),
                    "bar": _bar_data(bar),
                },
                execution_intent=execution_intent,
            )
            return PendingStopState.model_validate(base)

        current = PendingStopState.model_validate(base)
        cursor = bar.close_time_ms

    if as_of > expires_at:
        expired = current.model_dump(mode="python")
        expired.update(
            status="expired",
            changed_at_ms=expires_at,
            resolution_reason="intent_expired",
            evidence={
                "event": "expired",
                "changed_at_ms": expires_at,
                "reason": "intent_expired",
                "expires_at_ms": expires_at,
                "last_processed_close_time_ms": current.last_processed_close_time_ms,
            },
        )
        return PendingStopState.model_validate(expired)
    return current


__all__ = [
    "M1Bar",
    "PendingEntryError",
    "PendingStopState",
    "process_pending_stop",
]
