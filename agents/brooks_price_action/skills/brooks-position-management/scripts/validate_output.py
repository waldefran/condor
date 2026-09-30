#!/usr/bin/env python3
"""Deterministic validator for the Position Manager JSON contract."""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

ALLOWED_ACTIONS = {
    "HOLD", "PROTECT", "MOVE_PROTECTION", "TAKE_PARTIAL", "REDUCE",
    "CLOSE", "CLOSE_ALL", "HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE",
    "REMOVE_HEDGE", "CANCEL_ORDER", "REPLACE_ORDER", "RECONCILE_STATE",
    "REQUEST_MARKET_ANALYSIS", "MANAGEMENT_BLOCKED",
}
V1_SCHEMA = "brooks.management-decision.v1"
V2_SCHEMA = "brooks.management-decision.v2"
PM_OUTPUT_SCHEMA_V1 = V1_SCHEMA
PM_OUTPUT_SCHEMA_V2 = V2_SCHEMA
HEDGE_ACTIONS = {"HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"}
POSITION_REQUIRED_ACTIONS = {
    "HOLD", "PROTECT", "MOVE_PROTECTION", "TAKE_PARTIAL", "REDUCE", "CLOSE",
    "HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE",
}
REQUIRED_KEYS = {
    "schema", "role", "decision_time_ms", "action", "position_ids", "reason",
    "evidence", "risk", "execution", "hedge_plan", "market_analysis_request",
    "conditions_that_change_action",
}
EVIDENCE_KEYS = {"observations", "evidence_for", "evidence_against"}
RISK_KEYS = {
    "exposure_before", "exposure_after", "protection_status", "costs_considered",
    "uncertainty",
}
EXECUTION_KEYS = {"orders", "cancel_order_ids", "replace_orders"}
ORDER_REQUEST_KEYS = {
    "type", "order_id", "position_id", "symbol", "side", "quantity",
    "reduce_only", "order_type", "price", "stop_price",
}
REPLACE_REQUEST_KEYS = {"order_id", "replacement"}
REPLACEMENT_FIELDS = {
    "type", "symbol", "side", "quantity", "reduce_only", "order_type",
    "price", "stop_price",
}
HEDGE_KEYS = {
    "objective", "size", "expected_effect_on_exposure", "costs",
    "unlock_condition", "failure_condition",
}
HEDGE_V2_KEYS = {
    "objective", "target_hedge_ratio", "main_position_id", "hedge_position_id",
    "ratio_basis", "expected_effect_on_exposure", "costs", "unlock_condition",
    "failure_condition",
}
RATIO_BASIS = "absolute_mark_notional"
CANONICAL_RATIO = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
REQUEST_KEYS = {"schema", "request_id", "symbol", "decision_time_ms", "timeframes", "market_fields"}
REQUEST_FIELDS = {"ordered_ohlc", "bar_by_bar", "decision_time"}
PROBABILITY = re.compile(
    r"(?:\b\d{1,3}(?:\.\d+)?\s?%\s*(?:chance|probability|odds|win\s+rate)|\b(?:probability|chance|odds|win\s+rate)\s*[:=]?\s*\d|\b\d+\s+out\s+of\s+\d|\bodds\s+\d+\s*:\s*\d)",
    re.IGNORECASE,
)
FORBIDDEN_MANAGEMENT_KEYS = {
    "market_context", "setup", "entry_mechanism", "portfolio_allocation",
    "forecast", "target_price", "probability",
}


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("output must be strict JSON with no prose or code fence") from exc


def _strings(errors: list[str], field: str, value: Any, *, required: bool = True) -> None:
    if not isinstance(value, list):
        errors.append(f"{field} must be an array")
        return
    if required and not value:
        errors.append(f"{field} must be non-empty")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field}[{index}] must be a non-empty string")


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _all_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_strings(child)]
    return []


def _find_keys(value: Any, forbidden: set[str], path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in forbidden:
                found.append(f"{path}.{key}")
            found.extend(_find_keys(child, forbidden, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_keys(child, forbidden, f"{path}[{index}]"))
    return found


def _validate_request(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("market_analysis_request must be an object")
        return
    if set(value) != REQUEST_KEYS:
        errors.append(f"market_analysis_request keys must be exactly {sorted(REQUEST_KEYS)}")
        return
    if value.get("schema") != "brooks.market-analysis-request.v1":
        errors.append("market_analysis_request has the wrong schema")
    for field in ("request_id", "symbol"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"market_analysis_request.{field} must be a non-empty string")
    if not isinstance(value.get("decision_time_ms"), int):
        errors.append("market_analysis_request.decision_time_ms must be an integer")
    if not isinstance(value.get("timeframes"), list) or not value["timeframes"] or not all(isinstance(x, str) and x.strip() for x in value["timeframes"]):
        errors.append("market_analysis_request.timeframes must be a non-empty string array")
    if not isinstance(value.get("market_fields"), list) or not value["market_fields"] or not all(x in REQUEST_FIELDS for x in value["market_fields"]):
        errors.append("market_analysis_request.market_fields must contain only allowed market fields")
    leaked = _find_keys(value, FORBIDDEN_MANAGEMENT_KEYS | {"positions", "open_orders", "position_id", "position_ids", "side", "entry_price", "quantity", "pnl", "hedge", "account", "reason"})
    errors.extend(f"market-analysis request leaks private/non-market key: {path}" for path in leaked)


def _validate_order_request(value: Any, field: str, errors: list[str], *, require_type: bool = True) -> None:
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return
    unknown = set(value) - ORDER_REQUEST_KEYS
    errors.extend(f"{field} has unknown key: {key}" for key in sorted(unknown))
    if require_type and (not isinstance(value.get("type"), str) or not value["type"].strip()):
        errors.append(f"{field}.type must be a non-empty string")
    for key in ("order_id", "position_id", "symbol", "side", "order_type"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            errors.append(f"{field}.{key} must be a non-empty string")
    if "quantity" in value:
        quantity = value["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, (str, int, float)) or (isinstance(quantity, str) and not quantity.strip()):
            errors.append(f"{field}.quantity must be a non-empty scalar")
    if "reduce_only" in value and not isinstance(value["reduce_only"], bool):
        errors.append(f"{field}.reduce_only must be boolean")
    for key in ("price", "stop_price"):
        if key in value and (isinstance(value[key], bool) or not isinstance(value[key], (str, int, float))):
            errors.append(f"{field}.{key} must be a scalar")


def _validate_replace_request(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return
    if set(value) != REPLACE_REQUEST_KEYS:
        errors.extend(f"{field} missing key: {key}" for key in sorted(REPLACE_REQUEST_KEYS - set(value)))
        errors.extend(f"{field} unexpected key: {key}" for key in sorted(set(value) - REPLACE_REQUEST_KEYS))
        return
    if not isinstance(value.get("order_id"), str) or not value["order_id"].strip():
        errors.append(f"{field}.order_id must be a non-empty string")
    replacement = value.get("replacement")
    if not isinstance(replacement, dict) or not replacement:
        errors.append(f"{field}.replacement must be a non-empty object")
    elif set(replacement) - REPLACEMENT_FIELDS:
        errors.extend(f"{field}.replacement has unknown key: {key}" for key in sorted(set(replacement) - REPLACEMENT_FIELDS))
    else:
        _validate_order_request(replacement, f"{field}.replacement", errors, require_type=False)


def validate_output(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {"ok": False, "errors": ["empty output"]}
    try:
        schema_probe = _parse(raw)
    except ValueError:
        schema_probe = None
    if isinstance(schema_probe, dict) and schema_probe.get("schema") == V2_SCHEMA:
        return validate_output_v2(schema_probe)
    try:
        parsed = _parse(raw)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}

    errors: list[str] = []
    if not isinstance(parsed, dict):
        return {"ok": False, "errors": ["output must be a JSON object"]}
    errors.extend(f"missing key: {key}" for key in sorted(REQUIRED_KEYS - parsed.keys()))
    errors.extend(f"unexpected key: {key}" for key in sorted(parsed.keys() - REQUIRED_KEYS))
    if parsed.get("schema") != "brooks.management-decision.v1":
        errors.append("schema must be brooks.management-decision.v1")
    if parsed.get("role") != "POSITION_MANAGER":
        errors.append("role must be POSITION_MANAGER")
    if not isinstance(parsed.get("decision_time_ms"), int):
        errors.append("decision_time_ms must be an integer")
    if parsed.get("action") not in ALLOWED_ACTIONS:
        errors.append(f"action must be one of {sorted(ALLOWED_ACTIONS)}")
    if not isinstance(parsed.get("position_ids"), list) or not all(isinstance(x, str) and x.strip() for x in parsed.get("position_ids", [])):
        errors.append("position_ids must be an array of non-empty strings")
    if parsed.get("action") in POSITION_REQUIRED_ACTIONS and not parsed.get("position_ids"):
        errors.append(f"{parsed.get('action')} requires at least one affected position_id")
    if not isinstance(parsed.get("reason"), str) or not parsed["reason"].strip():
        errors.append("reason must be a non-empty string")

    evidence = parsed.get("evidence")
    if not isinstance(evidence, dict):
        errors.append("evidence must be an object")
    else:
        errors.extend(f"evidence missing key: {key}" for key in sorted(EVIDENCE_KEYS - evidence.keys()))
        errors.extend(f"evidence unexpected key: {key}" for key in sorted(evidence.keys() - EVIDENCE_KEYS))
        for field in EVIDENCE_KEYS:
            _strings(errors, f"evidence.{field}", evidence.get(field))

    risk = parsed.get("risk")
    if not isinstance(risk, dict):
        errors.append("risk must be an object")
    else:
        errors.extend(f"risk missing key: {key}" for key in sorted(RISK_KEYS - risk.keys()))
        errors.extend(f"risk unexpected key: {key}" for key in sorted(risk.keys() - RISK_KEYS))
        for field in ("exposure_before", "exposure_after", "costs_considered"):
            _strings(errors, f"risk.{field}", risk.get(field), required=field != "costs_considered" or True)
        if risk.get("protection_status") not in {"adequate", "inadequate", "unknown", "not_applicable"}:
            errors.append("risk.protection_status has an invalid value")
        if risk.get("uncertainty") not in {"high", "medium", "low"}:
            errors.append("risk.uncertainty has an invalid value")

    execution = parsed.get("execution")
    if not isinstance(execution, dict):
        errors.append("execution must be an object")
    else:
        errors.extend(f"execution missing key: {key}" for key in sorted(EXECUTION_KEYS - execution.keys()))
        errors.extend(f"execution unexpected key: {key}" for key in sorted(execution.keys() - EXECUTION_KEYS))
        for index, order in enumerate(execution.get("orders", []) if isinstance(execution.get("orders"), list) else []):
            _validate_order_request(order, f"execution.orders[{index}]", errors)
        for index, replacement in enumerate(execution.get("replace_orders", []) if isinstance(execution.get("replace_orders"), list) else []):
            _validate_replace_request(replacement, f"execution.replace_orders[{index}]", errors)
        for field in ("orders", "replace_orders"):
            if not isinstance(execution.get(field), list):
                errors.append(f"execution.{field} must be an array")
        if not isinstance(execution.get("cancel_order_ids"), list) or not all(isinstance(x, str) and x.strip() for x in execution.get("cancel_order_ids", [])):
            errors.append("execution.cancel_order_ids must be an array of strings")

    action = parsed.get("action")
    hedge_plan = parsed.get("hedge_plan")
    if action in HEDGE_ACTIONS:
        if not isinstance(hedge_plan, dict):
            errors.append("hedge actions require hedge_plan")
        else:
            errors.extend(f"hedge_plan missing key: {key}" for key in sorted(HEDGE_KEYS - hedge_plan.keys()))
            errors.extend(f"hedge_plan unexpected key: {key}" for key in sorted(hedge_plan.keys() - HEDGE_KEYS))
            for field in HEDGE_KEYS:
                value = hedge_plan.get(field)
                if field == "costs":
                    _strings(errors, "hedge_plan.costs", value)
                elif not isinstance(value, str) or not value.strip():
                    errors.append(f"hedge_plan.{field} must be a non-empty string")
    elif hedge_plan is not None:
        errors.append("hedge_plan must be null for non-hedge actions")

    request = parsed.get("market_analysis_request")
    if action == "REQUEST_MARKET_ANALYSIS":
        if request is None:
            errors.append("REQUEST_MARKET_ANALYSIS requires market_analysis_request")
        else:
            _validate_request(request, errors)
    elif request is not None:
        errors.append("market_analysis_request must be null unless action is REQUEST_MARKET_ANALYSIS")

    _strings(errors, "conditions_that_change_action", parsed.get("conditions_that_change_action"))
    leaked = _find_keys(parsed, FORBIDDEN_MANAGEMENT_KEYS)
    errors.extend(f"management output contains Trader/forecast key: {path}" for path in leaked)
    if PROBABILITY.search(" ".join(_all_strings(parsed))):
        errors.append("numeric probability or odds detected; use qualitative uncertainty")
    return {"ok": not errors, "errors": errors, "parsed": parsed}


def _parse_v2_input(raw_or_parsed: Any) -> tuple[Any | None, list[str]]:
    if isinstance(raw_or_parsed, dict):
        return raw_or_parsed, []
    if not isinstance(raw_or_parsed, str) or not raw_or_parsed.strip():
        return None, ["empty output"]
    try:
        return _parse(raw_or_parsed), []
    except ValueError as exc:
        return None, [str(exc)]


def _validate_v2_ratio(value: Any, field: str, errors: list[str]) -> Decimal | None:
    if not isinstance(value, str) or not CANONICAL_RATIO.fullmatch(value):
        errors.append(f"{field} must be a canonical decimal string")
        return None
    try:
        ratio = Decimal(value)
    except InvalidOperation:
        errors.append(f"{field} must be a valid decimal string")
        return None
    if ratio < Decimal("0") or ratio > Decimal("1"):
        errors.append(f"{field} must be between 0 and 1 inclusive")
    return ratio


def _validate_v2_hedge_plan(
    action: Any,
    hedge_plan: Any,
    position_ids: Any,
    errors: list[str],
) -> None:
    if action in HEDGE_ACTIONS:
        if not isinstance(hedge_plan, dict):
            errors.append("hedge actions require hedge_plan")
            return
        errors.extend(f"hedge_plan missing key: {key}" for key in sorted(HEDGE_V2_KEYS - hedge_plan.keys()))
        errors.extend(f"hedge_plan unexpected key: {key}" for key in sorted(hedge_plan.keys() - HEDGE_V2_KEYS))
        for field in ("objective", "expected_effect_on_exposure", "unlock_condition", "failure_condition"):
            value = hedge_plan.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"hedge_plan.{field} must be a non-empty string")
        costs = hedge_plan.get("costs")
        _strings(errors, "hedge_plan.costs", costs)
        if hedge_plan.get("ratio_basis") != RATIO_BASIS:
            errors.append("hedge_plan.ratio_basis must be absolute_mark_notional")

        target_ratio = _validate_v2_ratio(
            hedge_plan.get("target_hedge_ratio"),
            "hedge_plan.target_hedge_ratio",
            errors,
        )
        main_id = hedge_plan.get("main_position_id")
        if not isinstance(main_id, str) or not main_id.strip():
            errors.append("hedge_plan.main_position_id must be a non-empty string")
        hedge_id = hedge_plan.get("hedge_position_id")
        if hedge_id is not None and (not isinstance(hedge_id, str) or not hedge_id.strip()):
            errors.append("hedge_plan.hedge_position_id must be a non-empty string or null")
        if action in {"INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"} and hedge_id is None:
            errors.append(f"{action} requires hedge_plan.hedge_position_id")
        if action == "HEDGE" and hedge_id is not None:
            errors.append("HEDGE requires hedge_plan.hedge_position_id to be null")

        if target_ratio is not None:
            if action in {"HEDGE", "INCREASE_HEDGE"} and target_ratio <= Decimal("0"):
                errors.append(f"{action} requires target_hedge_ratio > 0")
            elif action == "REDUCE_HEDGE" and target_ratio <= Decimal("0"):
                errors.append("REDUCE_HEDGE requires target_hedge_ratio > 0")
            elif action == "REMOVE_HEDGE" and hedge_plan.get("target_hedge_ratio") != "0":
                errors.append("REMOVE_HEDGE requires target_hedge_ratio == '0'")

        if isinstance(position_ids, list):
            if isinstance(main_id, str) and main_id.strip() and main_id not in position_ids:
                errors.append("hedge_plan.main_position_id must appear in position_ids")
            if isinstance(hedge_id, str) and hedge_id.strip() and hedge_id not in position_ids:
                errors.append("hedge_plan.hedge_position_id must appear in position_ids")
    elif hedge_plan is not None:
        errors.append("hedge_plan must be null for non-hedge actions")


def validate_output_v2(raw_or_parsed: Any) -> dict[str, Any]:
    """Validate a V2 management decision from raw JSON or an object."""
    parsed, parse_errors = _parse_v2_input(raw_or_parsed)
    if parse_errors:
        return {"ok": False, "errors": parse_errors}
    if not isinstance(parsed, dict):
        return {"ok": False, "errors": ["output must be a JSON object"]}

    # Match the production host rather than maintaining a second V2 contract.
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
    from pydantic import ValidationError
    from condor.brooks.contracts import ManagementDecisionV2

    try:
        ManagementDecisionV2.model_validate(parsed)
    except ValidationError as exc:
        return {"ok": False, "errors": [str(exc)], "parsed": parsed}
    errors: list[str] = []
    leaked = _find_keys(parsed, FORBIDDEN_MANAGEMENT_KEYS)
    errors.extend(f"management output contains Trader/forecast key: {path}" for path in leaked)
    if PROBABILITY.search(" ".join(_all_strings(parsed))):
        errors.append("numeric probability or odds detected; use qualitative uncertainty")
    return {"ok": not errors, "errors": errors, "parsed": parsed}


validate_management_decision_v2 = validate_output_v2


def validate_management_decision(raw: Any) -> dict[str, Any]:
    """Dispatch the versioned management-decision validator by schema."""
    if isinstance(raw, dict) and raw.get("schema") == V2_SCHEMA:
        return validate_output_v2(raw)
    if isinstance(raw, dict):
        return validate_output(json.dumps(raw))
    return validate_output(raw)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_output.py <raw-output-file>", file=sys.stderr)
        return 2
    result = validate_output(open(sys.argv[1], encoding="utf-8").read())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
