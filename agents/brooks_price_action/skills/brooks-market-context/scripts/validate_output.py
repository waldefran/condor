#!/usr/bin/env python3
"""Deterministic validator for the Brooks market-context JSON contract."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

PRIMARY_REGIMES = {"bull-trend", "bear-trend", "trading-range", "transition-unclear"}
PHASES = {"breakout-spike", "channel", "range", "transition", "unclear"}
PRESSURE = {"bull", "bear", "balanced", "unclear"}
DIRECTIONS = {"long", "short", "unclear"}
RELEVANCE = {"high", "medium", "low"}
CONFIDENCE = {"high", "medium", "low"}
REQUIRED = {
    "primary_regime", "phase", "breakout_mode", "directional_pressure",
    "always_in", "always_in_relevance", "confidence", "evidence_for",
    "observations", "evidence_against", "transition_conditions", "missing_information",
}
PROBABILITY = re.compile(r"(?:\b\d{1,3}(?:\.\d+)?\s?%|\b(?:probability|chance|odds)\s*[:=]?\s*\d|\b\d+\s+out\s+of\s+\d)", re.I)


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("output must be strict JSON with no prose or code fence") from exc


def validate_output(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {"ok": False, "errors": ["empty output"]}
    try:
        parsed = _parse(raw)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}
    errors: list[str] = []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("market_context"), dict):
        return {"ok": False, "errors": ["output must contain a market_context object"]}
    context = parsed["market_context"]
    errors.extend(f"missing key: {key}" for key in sorted(REQUIRED - context.keys()))
    allowed = REQUIRED | {"broader_context"}
    errors.extend(f"unexpected key: {key}" for key in sorted(context.keys() - allowed))
    if set(parsed) != {"market_context"}:
        errors.append("output must contain only the market_context key")
    enums = {
        "primary_regime": PRIMARY_REGIMES, "phase": PHASES,
        "directional_pressure": PRESSURE, "always_in": DIRECTIONS,
        "always_in_relevance": RELEVANCE, "confidence": CONFIDENCE,
    }
    for field, choices in enums.items():
        if context.get(field) not in choices:
            errors.append(f"{field} must be one of {sorted(choices)}")
    breakout_mode = context.get("breakout_mode")
    if not isinstance(breakout_mode, bool) and breakout_mode != "unclear":
        errors.append("breakout_mode must be boolean or 'unclear'")
    for field in ("observations", "evidence_for", "evidence_against", "transition_conditions", "missing_information"):
        value = context.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            errors.append(f"{field} must be an array of non-empty strings")
        elif field != "missing_information" and not value:
            errors.append(f"{field} must be non-empty")
    strings = []
    for field in ("observations", "evidence_for", "evidence_against", "transition_conditions", "missing_information"):
        value = context.get(field)
        if isinstance(value, list):
            strings.extend(item for item in value if isinstance(item, str))
    if PROBABILITY.search(" ".join(strings)):
        errors.append("numeric probability or odds detected")
    return {"ok": not errors, "errors": errors, "parsed": parsed}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_output.py <raw-output-file>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        result = validate_output(handle.read())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
