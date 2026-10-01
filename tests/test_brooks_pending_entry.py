"""Focused safety checks for close-confirmed pending stop activation."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from condor.brooks.contracts import TradeIntentV2
from scripts.brooks_pending_entry import (
    M1Bar,
    PendingEntryError,
    PendingStopState,
    process_pending_stop,
)


DECISION_TIME = 3_599_999
AVAILABLE_AT = 3_600_123


def intent(side: str = "long") -> dict:
    is_long = side == "long"
    trigger_price, invalidation_price = ("101", "99") if is_long else ("99", "101")
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "ENTER_LONG" if is_long else "ENTER_SHORT",
        "symbol": "BTC-USDT",
        "decision_time_ms": DECISION_TIME,
        "market_context": {"trend": "up" if is_long else "down"},
        "setup": {"trigger_status": "pending"},
        "decision_timeframe": "M15",
        "context_timeframes_used": ["H1", "M15"],
        "entry_mechanism": "breakout",
        "trigger": {
            "kind": "stop",
            "direction": "above" if is_long else "below",
            "reference": "bar extreme",
            "price_field": "high" if is_long else "low",
            "price": trigger_price,
            "source": {
                "timeframe": "M15",
                "bar_index": 0,
                "open_time_ms": 2_700_000,
                "close_time_ms": DECISION_TIME,
            },
        },
        "invalidation": {
            "reference": "structural stop",
            "price_field": "low" if is_long else "high",
            "price": invalidation_price,
            "source": {
                "timeframe": "M15",
                "bar_index": 0,
                "open_time_ms": 2_700_000,
                "close_time_ms": DECISION_TIME,
            },
        },
        "evidence_for": ["setup could continue"],
        "evidence_against": ["breakout may fail"],
        "qualitative_confidence": "medium",
        "uncertainty": ["follow through"],
        "conditions_that_change_market_read": ["breakout fails"],
    }


def bar(
    open_time_ms: int,
    *,
    open: str = "100",
    high: str = "100.5",
    low: str = "100",
    close: str = "100.2",
    closed: bool = True,
) -> dict:
    return {
        "open_time_ms": open_time_ms,
        "close_time_ms": open_time_ms + 59_999,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "closed": closed,
    }


def test_activation_skips_forming_bar_and_preserves_original_intent():
    original = intent()
    pending = TradeIntentV2.model_validate(original)
    before = pending.model_dump(mode="json")
    original_before = deepcopy(original)
    result = process_pending_stop(
        pending,
        correlation_id="replay-1",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_719_999,
        closed_bars=[
            # This candle was already forming when the Trader completed.
            bar(3_600_000, high="104", low="100", close="103"),
            M1Bar(
                3_660_000,
                3_719_999,
                Decimal("100"),
                Decimal("103"),
                Decimal("100"),
                Decimal("102"),
            ),
        ],
    )

    assert result.status == "triggered"
    assert result.changed_at_ms == 3_719_999
    assert result.evidence["execution_reference_price"] == "102"
    assert result.evidence["bar"]["timeframe"] == "M1"
    assert result.execution_intent["setup"]["trigger_status"] == "triggered"
    derived = deepcopy(result.execution_intent)
    derived["setup"]["trigger_status"] = "pending"
    assert derived == before
    assert original == original_before
    assert pending.setup.trigger_status == "pending"


@pytest.mark.parametrize(
    ("side", "same_bar"),
    [
        ("long", bar(3_660_000, high="102", low="98", close="101.5")),
        ("short", bar(3_660_000, high="102", low="98", close="98.5")),
    ],
)
def test_invalidation_cancels_before_same_bar_trigger(side, same_bar):
    result = process_pending_stop(
        intent(side),
        correlation_id="same-bar",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_719_999,
        closed_bars=[same_bar],
    )
    assert result.status == "canceled"
    assert result.resolution_reason == "invalidation_touched_before_activation"
    assert result.evidence["trigger_crossed_same_bar"] is True
    assert result.execution_intent is None


@pytest.mark.parametrize(
    ("side", "equal_extreme"),
    [
        ("long", bar(3_660_000, high="101", low="100", close="101")),
        ("short", bar(3_660_000, high="100", low="99", close="99")),
    ],
)
def test_trigger_extreme_equality_does_not_activate(side, equal_extreme):
    result = process_pending_stop(
        intent(side),
        correlation_id="strict-cross",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_719_999,
        closed_bars=[equal_extreme],
    )
    assert result.status == "pending"
    assert result.last_processed_close_time_ms == 3_719_999


def test_wick_cross_without_close_confirmation_stays_pending():
    result = process_pending_stop(
        intent(),
        correlation_id="no-close-confirmation",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_719_999,
        closed_bars=[bar(3_660_000, high="103", low="100", close="101")],
    )
    assert result.status == "pending"
    assert result.execution_intent is None


def test_expiry_does_not_use_a_candle_that_closes_after_deadline():
    result = process_pending_stop(
        intent(),
        correlation_id="expiry",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_779_999,
        max_intent_age_ms=120_000,
        closed_bars=[
            bar(3_600_000, high="100.5", close="100.2"),
            bar(3_660_000, high="100.5", close="100.2"),
            bar(3_720_000, high="104", low="100", close="103"),
        ],
    )
    assert result.status == "expired"
    assert result.expires_at_ms == 3_719_999
    assert result.last_processed_close_time_ms == 3_719_999
    assert result.execution_intent is None


def test_checkpoint_replay_is_idempotent_and_json_serializable():
    first_bar = bar(3_660_000, high="100.5", low="100", close="100.2")
    first = process_pending_stop(
        intent(),
        correlation_id="resume",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_719_999,
        closed_bars=[first_bar],
    )
    resumed = process_pending_stop(
        intent(),
        correlation_id="resume",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_779_999,
        closed_bars=[
            first_bar,
            bar(3_720_000, high="103", low="100", close="102"),
        ],
        state=first.model_dump(mode="json"),
    )
    repeated = process_pending_stop(
        intent(),
        correlation_id="resume",
        available_at_ms=AVAILABLE_AT,
        as_of_ms=3_779_999,
        closed_bars=[bar(3_720_000, high="103", low="100", close="102")],
        state=resumed,
    )

    assert first.status == "pending"
    assert resumed.status == repeated.status == "triggered"
    assert resumed.changed_at_ms == repeated.changed_at_ms == 3_779_999
    assert resumed.model_dump(mode="json") == repeated.model_dump(mode="json")
    # Checkpoints remain primitive JSON and can be persisted directly.
    assert (
        json.loads(json.dumps(resumed.model_dump(mode="json")))
        == resumed.model_dump(mode="json")
    )
    assert isinstance(resumed, PendingStopState)


def test_rejects_future_partial_and_nonfinite_bars():
    with pytest.raises(PendingEntryError, match="as_of_ms"):
        process_pending_stop(
            intent(),
            correlation_id="future",
            available_at_ms=AVAILABLE_AT,
            as_of_ms=3_659_999,
            closed_bars=[bar(3_660_000, high="103", close="102")],
        )
    with pytest.raises(PendingEntryError, match="closed"):
        process_pending_stop(
            intent(),
            correlation_id="partial",
            available_at_ms=AVAILABLE_AT,
            as_of_ms=3_659_999,
            closed_bars=[bar(3_600_000, high="103", close="102", closed=False)],
        )
    with pytest.raises(PendingEntryError, match="finite"):
        process_pending_stop(
            intent(),
            correlation_id="nonfinite",
            available_at_ms=AVAILABLE_AT,
            as_of_ms=3_659_999,
            closed_bars=[bar(3_600_000, high="NaN", close="102")],
        )
