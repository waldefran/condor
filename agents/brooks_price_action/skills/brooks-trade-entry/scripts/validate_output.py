#!/usr/bin/env python3
"""Deterministic validator for the Trader market-only Trade Intent contract."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

SCHEMA = "brooks.trade-intent.v2"
LEGACY_SCHEMA = "brooks.trade-intent.v1"
DECISIONS = {"ENTER_LONG", "ENTER_SHORT", "NO_TRADE"}
MECHANISMS = {"continuation", "breakout", "breakout_pullback", "reversal", "none", "unclear"}
CONFIDENCE = {"high", "medium", "low"}
TRIGGER_STATUS = {"pending", "triggered", "present", "absent", "failed", "stale", "unknown"}
SIGNAL_QUALITY = {"clear", "weak", "failed", "absent", "unknown"}
LOCATION = {"favorable", "neutral", "poor", "unknown"}
NO_TRADE_REASONS = {
    "no_trigger", "poor_location", "weak_signal", "failed_breakout",
    "opposing_pressure", "insufficient_data", "timeframe_conflict",
    "stale_or_late", "missing_invalidation", "balanced_context",
}
V1_KEYS = {
    "schema", "role", "decision", "symbol", "decision_time_ms", "market_context",
    "setup", "entry_mechanism", "entry_reference_or_zone", "structural_invalidation",
    "evidence_for", "evidence_against", "qualitative_confidence", "uncertainty",
    "conditions_that_change_market_read",
}
V2_KEYS = {
    "schema", "role", "decision", "symbol", "decision_time_ms", "market_context",
    "setup", "decision_timeframe", "context_timeframes_used", "entry_mechanism",
    "trigger", "invalidation", "evidence_for", "evidence_against",
    "qualitative_confidence", "uncertainty", "conditions_that_change_market_read",
}
V1_ARRAY_FIELDS = {
    "entry_reference_or_zone", "structural_invalidation", "evidence_for",
    "evidence_against", "uncertainty", "conditions_that_change_market_read",
}
V2_ARRAY_FIELDS = {
    "context_timeframes_used", "evidence_for", "evidence_against", "uncertainty",
    "conditions_that_change_market_read",
}
SOURCE_KEYS = {"timeframe", "bar_index", "open_time_ms", "close_time_ms"}
TRIGGER_KEYS = {"kind", "direction", "reference", "price_field", "price", "source"}
INVALIDATION_KEYS = {"reference", "price_field", "price", "source"}
PRICE_FIELDS = {"open", "high", "low", "close"}
DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
PRIVATE_KEYS = {
    "account", "balance", "equity", "available_margin", "margin", "leverage",
    "fees", "funding", "positions", "position", "position_state", "open_positions",
    "open_orders", "orders", "fills", "entry_price", "entry_prices", "position_side",
    "position_size", "quantity", "unrealized_pnl", "realized_pnl", "pnl", "loss_streak",
    "trade_history", "hedges", "hedge", "management_history", "pm_opinion", "pm_intent",
    "portfolio", "available_balance",
}
FUTURE_KEYS = {
    "outcome_" + "bars", "future_" + "bars", "future_observation", "profitability",
    "realized_return", "mfe", "mae", "final_pnl",
}
NUMERIC_PROBABILITY = re.compile(
    r"(?:\b\d{1,3}(?:\.\d+)?\s?%\s*(?:chance|probability|odds|win\s+rate)|\b(?:probability|chance|odds|win\s+rate)\s*[:=]?\s*\d|\b\d+\s+out\s+of\s+\d|\bodds\s+\d+\s*:\s*\d)",
    re.IGNORECASE,
)


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("output must be strict JSON with no prose or code fence") from exc


def _walk_keys(value: Any, forbidden: set[str], path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in forbidden:
                found.append(f"{path}.{key}")
            found.extend(_walk_keys(child, forbidden, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_keys(child, forbidden, f"{path}[{index}]"))
    return found


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _all_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_strings(child)]
    return []


def _strings(errors: list[str], field: str, value: Any, *, nonempty: bool) -> None:
    if not isinstance(value, list):
        errors.append(f"{field} must be an array")
        return
    if nonempty and not value:
        errors.append(f"{field} must be non-empty")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field}[{index}] must be a non-empty string")
        elif field in {"evidence_for", "evidence_against"} and len(item.strip()) < 12:
            errors.append(f"{field}[{index}] is too short to be substantive evidence")


def _source(errors: list[str], field: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return
    errors.extend(f"{field} missing key: {key}" for key in sorted(SOURCE_KEYS - value.keys()))
    errors.extend(f"{field} unexpected key: {key}" for key in sorted(value.keys() - SOURCE_KEYS))
    if not isinstance(value.get("timeframe"), str) or not value["timeframe"].strip():
        errors.append(f"{field}.timeframe must be a non-empty string")
    for key in ("bar_index", "open_time_ms", "close_time_ms"):
        if not isinstance(value.get(key), int) or isinstance(value.get(key), bool):
            errors.append(f"{field}.{key} must be an integer")
    if isinstance(value.get("bar_index"), int) and value["bar_index"] < 0:
        errors.append(f"{field}.bar_index must be non-negative")
    if isinstance(value.get("open_time_ms"), int) and isinstance(value.get("close_time_ms"), int) and value["close_time_ms"] < value["open_time_ms"]:
        errors.append(f"{field}.close_time_ms precedes open_time_ms")


def _price_reference(errors: list[str], field: str, value: Any, *, trigger: bool) -> None:
    expected = TRIGGER_KEYS if trigger else INVALIDATION_KEYS
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return
    errors.extend(f"{field} missing key: {key}" for key in sorted(expected - value.keys()))
    errors.extend(f"{field} unexpected key: {key}" for key in sorted(value.keys() - expected))
    if trigger and value.get("kind") not in {"market", "stop", "limit"}:
        errors.append(f"{field}.kind is invalid")
    if trigger and value.get("direction") not in {"at", "above", "below"}:
        errors.append(f"{field}.direction is invalid")
    if not isinstance(value.get("reference"), str) or not value["reference"].strip():
        errors.append(f"{field}.reference must be a non-empty string")
    if value.get("price_field") not in PRICE_FIELDS:
        errors.append(f"{field}.price_field is invalid")
    if not isinstance(value.get("price"), str) or not DECIMAL.fullmatch(value["price"]):
        errors.append(f"{field}.price must be a non-negative plain decimal string")
    _source(errors, f"{field}.source", value.get("source"))


def validate_output(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {"ok": False, "errors": ["empty output"]}
    try:
        parsed = _parse(raw)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}
    if not isinstance(parsed, dict):
        return {"ok": False, "errors": ["output must be a JSON object"]}

    errors: list[str] = []
    schema = parsed.get("schema")
    expected_keys = V2_KEYS if schema == SCHEMA else V1_KEYS if schema == LEGACY_SCHEMA else V2_KEYS
    errors.extend(f"missing key: {key}" for key in sorted(expected_keys - parsed.keys()))
    errors.extend(f"unexpected key: {key}" for key in sorted(parsed.keys() - expected_keys))
    if schema not in {SCHEMA, LEGACY_SCHEMA}:
        errors.append(f"schema must be {SCHEMA}")
    if parsed.get("role") != "TRADER":
        errors.append("role must be TRADER")
    if parsed.get("decision") not in DECISIONS:
        errors.append(f"decision must be one of {sorted(DECISIONS)}")
    if not isinstance(parsed.get("symbol"), str) or not parsed["symbol"].strip():
        errors.append("symbol must be a non-empty string")
    if not isinstance(parsed.get("decision_time_ms"), int) or isinstance(parsed.get("decision_time_ms"), bool):
        errors.append("decision_time_ms must be an integer")
    if parsed.get("entry_mechanism") not in MECHANISMS:
        errors.append(f"entry_mechanism must be one of {sorted(MECHANISMS)}")
    if parsed.get("qualitative_confidence") not in CONFIDENCE:
        errors.append(f"qualitative_confidence must be one of {sorted(CONFIDENCE)}")

    for field in ("market_context", "setup"):
        if parsed.get(field) is not None and not isinstance(parsed.get(field), dict):
            errors.append(f"{field} must be an object or null")
    setup = parsed.get("setup")
    if isinstance(setup, dict):
        if "type" in setup:
            if schema == SCHEMA:
                if setup["type"] is not None and not isinstance(setup["type"], str):
                    errors.append("setup.type must be a string or null")
            elif setup["type"] not in {None, *MECHANISMS}:
                errors.append("setup.type is invalid")
        if "trigger_status" in setup and setup["trigger_status"] not in TRIGGER_STATUS:
            errors.append("setup.trigger_status is invalid")
        if "signal_quality" in setup and setup["signal_quality"] not in SIGNAL_QUALITY:
            errors.append("setup.signal_quality is invalid")
        if "location_assessment" in setup and setup["location_assessment"] not in LOCATION:
            errors.append("setup.location_assessment is invalid")
        reason = setup.get("no_trade_reason")
        if reason is not None and reason not in NO_TRADE_REASONS:
            errors.append("setup.no_trade_reason is invalid")
        if parsed.get("decision") == "NO_TRADE" and reason is None and setup:
            errors.append("NO_TRADE setup must state no_trade_reason or use setup=null")
        if parsed.get("decision") != "NO_TRADE" and reason is not None:
            errors.append("entry decisions cannot contain setup.no_trade_reason")

    array_fields = V2_ARRAY_FIELDS if schema == SCHEMA else V1_ARRAY_FIELDS
    for field in array_fields:
        _strings(
            errors,
            field,
            parsed.get(field),
            nonempty=field not in {"entry_reference_or_zone", "structural_invalidation", "context_timeframes_used"},
        )

    if schema == SCHEMA:
        decision_timeframe = parsed.get("decision_timeframe")
        if decision_timeframe is not None and (not isinstance(decision_timeframe, str) or not decision_timeframe.strip()):
            errors.append("decision_timeframe must be a non-empty string or null")
        trigger_value = parsed.get("trigger")
        invalidation_value = parsed.get("invalidation")
        if trigger_value is not None:
            _price_reference(errors, "trigger", trigger_value, trigger=True)
        if invalidation_value is not None:
            _price_reference(errors, "invalidation", invalidation_value, trigger=False)

    decision = parsed.get("decision")
    if decision in {"ENTER_LONG", "ENTER_SHORT"}:
        if parsed.get("entry_mechanism") in {"none", "unclear"}:
            errors.append("entry decisions require a specified entry_mechanism")
        if schema == SCHEMA:
            if parsed.get("decision_timeframe") != "M15":
                errors.append("entry decisions require decision_timeframe=M15")
            if not parsed.get("trigger"):
                errors.append("entry decisions require trigger")
            if not parsed.get("invalidation"):
                errors.append("entry decisions require invalidation")
            for field in ("trigger", "invalidation"):
                reference = parsed.get(field)
                source = reference.get("source") if isinstance(reference, dict) else None
                if isinstance(source, dict) and source.get("timeframe") != "M15":
                    errors.append(f"{field}.source.timeframe must be M15")
            if not parsed.get("context_timeframes_used"):
                errors.append("entry decisions require context_timeframes_used")
            setup_obj = parsed.get("setup")
            if isinstance(setup_obj, dict):
                trig_status = setup_obj.get("trigger_status")
                trig = parsed.get("trigger")
                if trig_status == "pending" and isinstance(trig, dict):
                    if trig.get("kind") not in {"stop", "limit"}:
                        errors.append("pending entry decisions require stop or limit trigger kind")
                    if trig.get("kind") == "stop" and trig.get("direction") not in {"above", "below"}:
                        errors.append("pending stop entries require above or below direction")
        else:
            if not parsed.get("entry_reference_or_zone"):
                errors.append("entry decisions require entry_reference_or_zone")
            if not parsed.get("structural_invalidation"):
                errors.append("entry decisions require structural_invalidation")
    if schema == SCHEMA and decision == "NO_TRADE" and any(parsed.get(field) is not None for field in ("decision_timeframe", "trigger", "invalidation")):
        errors.append("NO_TRADE requires null decision_timeframe, trigger, and invalidation")
    if decision == "NO_TRADE" and parsed.get("entry_mechanism") not in {"none", "unclear"}:
        setup_reason = setup.get("no_trade_reason") if isinstance(setup, dict) else None
        if setup_reason not in {"failed_breakout", "stale_or_late", "poor_location", "weak_signal", "timeframe_conflict", "opposing_pressure"}:
            errors.append("NO_TRADE with an active entry_mechanism needs a compatible setup reason")

    errors.extend(
        f"Trader output leaks private/future key: {path}"
        for path in _walk_keys(parsed, PRIVATE_KEYS | FUTURE_KEYS)
    )
    if NUMERIC_PROBABILITY.search(" ".join(_all_strings(parsed))):
        errors.append("numeric probability or odds detected; use qualitative confidence only")
    return {"ok": not errors, "errors": errors, "parsed": parsed}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_output.py <raw-output-file>", file=sys.stderr)
        return 2
    result = validate_output(open(sys.argv[1], encoding="utf-8").read())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
