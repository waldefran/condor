"""Focused regressions for canonical and observed Brooks entry references."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from condor.brooks.contracts import TradeIntentV2
from condor.brooks.trader import TraderConsumer, _validate_references

_M15_MS = 900_000


def _bars(count: int, decision_time_ms: int) -> list[dict[str, object]]:
    return [
        {
            "open_time_ms": decision_time_ms - (count - index) * _M15_MS,
            "close_time_ms": decision_time_ms - (count - index - 1) * _M15_MS - 1,
            "open": "2637",
            "high": "2638",
            "low": "2636",
            "close": "2637.5",
            "closed": True,
        }
        for index in range(count)
    ]


def _entry(decision_time_ms: int, bars: list[dict[str, object]], index: int) -> dict:
    bar = bars[index]
    source = {
        "timeframe": "M15",
        "bar_index": index,
        "open_time_ms": bar["open_time_ms"],
        "close_time_ms": bar["close_time_ms"],
    }
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "ENTER_LONG",
        "symbol": "ETH-USDT",
        "decision_time_ms": decision_time_ms,
        "market_context": {},
        "setup": {
            "type": "breakout",
            "trigger_status": "pending",
            "signal_quality": "clear",
            "location_assessment": "favorable",
            "no_trade_reason": None,
        },
        "decision_timeframe": "M15",
        "context_timeframes_used": ["H1", "M15", "H4"],
        "entry_mechanism": "breakout",
        "trigger": {
            "kind": "stop",
            "direction": "above",
            "reference": "signal_bar_high",
            "price_field": "high",
            "price": "2638",
            "source": source,
        },
        "invalidation": {
            "reference": "signal_bar_low",
            "price_field": "low",
            "price": "2636",
            "source": source,
        },
        "evidence_for": ["The closed M15 bar defines the trigger."],
        "evidence_against": ["The breakout may fail."],
        "qualitative_confidence": "medium",
        "uncertainty": ["Follow-through is unknown."],
        "conditions_that_change_market_read": ["A close below the bar low."],
    }


def test_m15_alias_is_canonical_and_schema_rejects_other_entry_timeframes():
    decision = 120 * _M15_MS
    bars = _bars(120, decision)
    payload = _entry(decision, bars, 119)
    payload["decision_timeframe"] = "15m"
    payload["trigger"]["source"]["timeframe"] = "15m"
    payload["invalidation"]["source"]["timeframe"] = "15m"

    intent = TradeIntentV2.model_validate(payload)
    serialized = intent.model_dump(mode="json")
    assert serialized["decision_timeframe"] == "M15"
    assert serialized["trigger"]["source"]["timeframe"] == "M15"
    assert serialized["invalidation"]["source"]["timeframe"] == "M15"
    schema = TradeIntentV2.model_json_schema()
    assert schema["$defs"]["TriggerSource"]["properties"]["timeframe"]["const"] == "M15"
    assert schema["properties"]["decision_timeframe"]["anyOf"][0]["const"] == "M15"

    for timeframe in ("H1", "H4", "D1", "15M"):
        invalid = intent.model_dump(mode="json")
        invalid["trigger"]["source"]["timeframe"] = timeframe
        with pytest.raises(ValidationError):
            TradeIntentV2.model_validate(invalid)


def test_reference_uses_exact_decimal_value_and_keeps_index_time_identity():
    decision = 120 * _M15_MS
    bars = _bars(120, decision)
    windows = {"M15": {"bars": bars}}
    payload = _entry(decision, bars, 119)
    payload["trigger"]["price"] = "2638.00"
    payload["invalidation"]["price"] = "2636.0"
    _validate_references(TradeIntentV2.model_validate(payload), windows)

    payload["trigger"]["price"] = "2638.0001"
    with pytest.raises(ValueError, match="does not match any supplied closed bar"):
        _validate_references(TradeIntentV2.model_validate(payload), windows)

    canonical_intent = TradeIntentV2.model_validate(_entry(decision, bars, 119))
    for timeframe in ("H1", "H4", "D1"):
        source = canonical_intent.trigger.source.model_copy(
            update={"timeframe": timeframe}
        )
        trigger = canonical_intent.trigger.model_copy(update={"source": source})
        invalid = canonical_intent.model_copy(update={"trigger": trigger})
        with pytest.raises(ValueError, match="must cite M15"):
            _validate_references(invalid, windows)

    payload = _entry(decision, bars, 119)
    payload["trigger"]["source"]["bar_index"] = 118
    with pytest.raises(ValueError, match="does not match any supplied closed bar"):
        _validate_references(TradeIntentV2.model_validate(payload), windows)

    payload = _entry(decision, bars, 119)
    payload["invalidation"]["source"]["close_time_ms"] += 100
    with pytest.raises(ValueError, match="does not match any supplied closed bar"):
        _validate_references(TradeIntentV2.model_validate(payload), windows)


class _Source:
    def __init__(self, decision_time_ms: int):
        self.decision_time_ms = decision_time_ms

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int):
        assert symbol == "ETH-USDT" and timeframe == "15m"
        return _bars(limit, self.decision_time_ms)


@pytest.mark.asyncio
async def test_short_same_run_m15_tool_window_is_validated_as_observed():
    decision = 120 * _M15_MS
    initial = _bars(120, decision)
    observed = {"H1": [], "M15": [initial], "H4": [], "D1": []}
    consumer = TraderConsumer("test", _Source(decision), {}, None)
    tools = consumer._monitored_tools(
        "ETH-USDT", decision, {}, observed, lambda _: None
    )

    short_window = await tools["get_closed_candles"](
        symbol="ETH-USDT", timeframe="15m", limit=12
    )
    assert observed["M15"][-1] == short_window
    intent = TradeIntentV2.model_validate(_entry(decision, short_window, 4))
    with pytest.raises(ValueError, match="does not match any supplied closed bar"):
        _validate_references(intent, {"M15": [initial]})
    _validate_references(intent, observed)


def test_market_location_position_key_is_narrowly_allowed():
    decision = 120 * _M15_MS
    bars = _bars(120, decision)
    payload = _entry(decision, bars, 119)
    payload["decision"] = "NO_TRADE"
    payload["setup"] = {"no_trade_reason": "balanced_context"}
    payload["decision_timeframe"] = None
    payload["entry_mechanism"] = "none"
    payload["trigger"] = None
    payload["invalidation"] = None
    payload["market_context"] = {
        "m15_facts": {
            "position": "lower quarter of the tight range; 13.08 above the range low"
        }
    }
    TradeIntentV2.model_validate(payload)

    payload["market_context"]["m15_facts"]["position"] = {"side": "long"}
    with pytest.raises(
        ValidationError, match="private or future market field: position"
    ):
        TradeIntentV2.model_validate(payload)

    payload["market_context"] = {
        "position": "lower quarter of the tight range; 13.08 above the range low"
    }
    with pytest.raises(
        ValidationError, match="private or future market field: position"
    ):
        TradeIntentV2.model_validate(payload)
