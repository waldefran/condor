"""Comprehensive unit and adversarial tests for Brooks FEAT-014 REQUEST_MARKET_ANALYSIS.

Tests the strict market-only privacy boundary, recursive private leak detection,
request sanitization, write-tools prevention, anti-loop guard, and handshake orchestration.
"""

from __future__ import annotations

from typing import Any
import pytest

from condor.brooks.market_analysis import (
    ALLOWED_MARKET_FIELDS,
    DEFAULT_ALLOWED_TIMEFRAMES,
    FORBIDDEN_OPERATIONAL_TOOLS,
    PRIVATE_STATE_DENYLIST,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    TRADER_RESPONSE_DENYLIST,
    ForbiddenWriteToolError,
    MarketAnalysisBoundaryError,
    MarketAnalysisInvalidRequestError,
    MarketAnalysisLeakError,
    MarketAnalysisLoopBlockedError,
    MarketAnalysisRequestV1,
    MarketAnalysisResponseV1,
    MarketAnalysisTraderBoundaryViolation,
    MarketAnalysisUnsupportedTimeframeError,
    async_execute_fresh_market_analysis_handshake,
    check_pm_anti_loop_guard,
    create_market_analysis_response,
    execute_fresh_market_analysis_handshake,
    filter_read_only_tools,
    find_private_state_leaks,
    normalize_key,
    sanitize_market_analysis_request,
    validate_tools_for_market_analysis,
    verify_no_write_calls_recorded,
)


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def valid_request_dict() -> dict[str, Any]:
    return {
        "schema": REQUEST_SCHEMA,
        "request_id": "req-brooks-101",
        "symbol": "BTC-USDT",
        "decision_time_ms": 1700000000000,
        "timeframes": ["15m", "1h", "4h"],
        "market_fields": ["ordered_ohlc", "bar_by_bar", "decision_time"],
    }


def valid_trade_intent_dict() -> dict[str, Any]:
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "NO_TRADE",
        "symbol": "BTC-USDT",
        "decision_time_ms": 1700000000000,
        "market_context": {"bar_count": 30, "trend": "bull_channel"},
        "setup": {"type": None, "no_trade_reason": "no_trigger"},
        "decision_timeframe": None,
        "context_timeframes_used": ["1h", "4h"],
        "entry_mechanism": "none",
        "trigger": None,
        "invalidation": None,
        "evidence_for": ["Strong bull body on H4"],
        "evidence_against": ["Approaching major resistance on D1"],
        "qualitative_confidence": "medium",
        "uncertainty": ["Waiting for breakout follow-through"],
        "conditions_that_change_market_read": ["Close above 95000"],
    }


# ---------------------------------------------------------------------------
# Key Normalization Tests
# ---------------------------------------------------------------------------

def test_normalize_key_variants():
    assert normalize_key("accountBalance") == ("account_balance", ["account", "balance"])
    assert normalize_key("PositionSide") == ("position_side", ["position", "side"])
    assert normalize_key("user-sub-account") == ("user_sub_account", ["user", "sub", "account"])
    assert normalize_key("entry_price") == ("entry_price", ["entry", "price"])
    assert normalize_key("HedgeRatio") == ("hedge_ratio", ["hedge", "ratio"])
    assert normalize_key("pnl") == ("pnl", ["pnl"])


# ---------------------------------------------------------------------------
# Recursive Leak Detection Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "leaking_key,value",
    [
        ("account", "0x12345"),
        ("balance", "50000.00"),
        ("equity", "51200.00"),
        ("available_margin", "25000.00"),
        ("margin", "10000.00"),
        ("leverage", 10),
        ("fees", "12.50"),
        ("funding", "1.20"),
        ("positions", [{"id": "p-1"}]),
        ("position", {"symbol": "BTC-USDT"}),
        ("position_state", "OPEN"),
        ("position_id", "pos-999"),
        ("open_positions", 2),
        ("open_orders", []),
        ("orders", [{"order_id": "ord-1"}]),
        ("fills", []),
        ("entry", "91000.0"),
        ("entry_price", "91000.0"),
        ("entry_prices", ["91000.0"]),
        ("position_side", "LONG"),
        ("position_size", "0.5"),
        ("quantity", "0.5"),
        ("qty", "0.5"),
        ("unrealized_pnl", "+120.00"),
        ("realized_pnl", "+50.00"),
        ("pnl", "+170.00"),
        ("loss_streak", 0),
        ("trade_history", []),
        ("hedges", []),
        ("hedge", "SHORT 0.2"),
        ("hedge_plan", {"target": "0.3"}),
        ("hedge_ratio", "0.3"),
        ("target_hedge_ratio", "0.3"),
        ("management_history", []),
        ("pm_opinion", "bearish hedge needed"),
        ("pm_intent", "REDUCE"),
        ("pm_reason", "trend broken"),
        ("portfolio", {"nav": 100000}),
        ("available_balance", "40000"),
        ("action", "HEDGE"),
        ("reason", "stop breach"),
        ("execution", {"order": "SELL"}),
        ("protection_status", "inadequate"),
        ("intended_action", "CLOSE"),
    ],
)
def test_detects_all_section_24_and_harness_private_denylist_keys(leaking_key: str, value: Any):
    # Flat top-level
    obj = {"symbol": "BTC-USDT", leaking_key: value}
    leaks = find_private_state_leaks(obj)
    assert any(leaking_key in leak for leak in leaks), f"Failed to detect leak: {leaking_key}"


def test_recursive_leak_detection_nested_and_lists():
    # Deep nested structure
    nested_obj = {
        "symbol": "BTC-USDT",
        "market_meta": {
            "source": "exchange",
            "audit": {
                "records": [
                    {"valid": True},
                    {"valid": False, "portfolioBalance": "100000"},
                ]
            },
        },
    }
    leaks = find_private_state_leaks(nested_obj)
    assert leaks == ["$.market_meta.audit.records[1].portfolioBalance"]


def test_recursive_leak_detection_camel_case_and_tokens():
    obj = {
        "symbol": "BTC-USDT",
        "unrealizedPnl": "500",
        "userAccountId": "acc-10",
        "openOrderCount": 3,
        "currentHedgeRatio": "0.4",
    }
    leaks = find_private_state_leaks(obj)
    assert len(leaks) == 4
    assert "$.unrealizedPnl" in leaks
    assert "$.userAccountId" in leaks
    assert "$.openOrderCount" in leaks
    assert "$.currentHedgeRatio" in leaks


def test_recursive_leak_detection_embedded_json_string():
    obj = {
        "symbol": "BTC-USDT",
        "opaque_payload": '{"account_id": "12345", "safe_field": 42}',
    }
    leaks = find_private_state_leaks(obj)
    assert "$.opaque_payload.(json).account_id" in leaks


def test_clean_market_object_has_zero_leaks():
    clean = valid_request_dict()
    assert find_private_state_leaks(clean) == []


# ---------------------------------------------------------------------------
# Request Sanitization Tests
# ---------------------------------------------------------------------------

def test_sanitize_valid_request_reconstructs_clean_object():
    req = valid_request_dict()
    sanitized = sanitize_market_analysis_request(req)
    assert isinstance(sanitized, MarketAnalysisRequestV1)
    assert sanitized.schema == REQUEST_SCHEMA
    assert sanitized.request_id == "req-brooks-101"
    assert sanitized.symbol == "BTC-USDT"
    assert sanitized.decision_time_ms == 1700000000000
    assert sanitized.timeframes == ["15m", "1h", "4h"]
    assert sanitized.market_fields == ["ordered_ohlc", "bar_by_bar", "decision_time"]


def test_sanitize_drops_arbitrary_unapproved_fields():
    req = valid_request_dict()
    req["unapproved_extra_tag"] = "harmless_string"
    req["client_version"] = "2.0.1"

    sanitized = sanitize_market_analysis_request(req)
    dumped = sanitized.model_dump()
    assert "unapproved_extra_tag" not in dumped
    assert "client_version" not in dumped
    assert set(dumped.keys()) == {
        "schema", "request_id", "symbol", "decision_time_ms", "timeframes", "market_fields"
    }


def test_sanitize_rejects_private_state_leak():
    req = valid_request_dict()
    req["position_side"] = "LONG"

    with pytest.raises(MarketAnalysisLeakError) as exc_info:
        sanitize_market_analysis_request(req)
    assert "position_side" in str(exc_info.value)
    assert "$.position_side" in exc_info.value.leaks


def test_sanitize_rejects_none_or_non_mapping():
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(None)
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(["BTC-USDT"])
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request("not-an-object")


def test_sanitize_rejects_empty_or_whitespace_strings():
    req = valid_request_dict()
    req["request_id"] = "   "
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req)

    req2 = valid_request_dict()
    req2["symbol"] = ""
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req2)


def test_sanitize_rejects_invalid_decision_time():
    req = valid_request_dict()
    req["decision_time_ms"] = -1
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req)

    req["decision_time_ms"] = "1700000000000"
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req)

    req["decision_time_ms"] = True  # bool is int subclass
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req)


def test_sanitize_rejects_empty_or_unsupported_timeframes():
    req = valid_request_dict()
    req["timeframes"] = []
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req)

    req["timeframes"] = ["1s"]  # Not in allowed list
    with pytest.raises(MarketAnalysisUnsupportedTimeframeError) as exc_info:
        sanitize_market_analysis_request(req)
    assert "1s" in str(exc_info.value)

    # Custom allowed timeframes policy
    custom_policy = ["H1", "H4"]
    req["timeframes"] = ["15m"]
    with pytest.raises(MarketAnalysisUnsupportedTimeframeError):
        sanitize_market_analysis_request(req, allowed_timeframes=custom_policy)


def test_sanitize_rejects_empty_or_invalid_market_fields():
    req = valid_request_dict()
    req["market_fields"] = []
    with pytest.raises(MarketAnalysisInvalidRequestError):
        sanitize_market_analysis_request(req)

    req["market_fields"] = ["order_book_depth"]
    with pytest.raises(MarketAnalysisInvalidRequestError) as exc_info:
        sanitize_market_analysis_request(req)
    assert "order_book_depth" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Response Creation Tests
# ---------------------------------------------------------------------------

def test_create_market_analysis_response_success():
    req = sanitize_market_analysis_request(valid_request_dict())
    intent = valid_trade_intent_dict()

    resp = create_market_analysis_response(req, intent)
    assert isinstance(resp, MarketAnalysisResponseV1)
    assert resp.schema == RESPONSE_SCHEMA
    assert resp.request_id == req.request_id
    assert resp.symbol == req.symbol
    assert resp.decision_time_ms == req.decision_time_ms
    assert resp.trade_intent["decision"] == "NO_TRADE"
    assert find_private_state_leaks(resp.model_dump(), TRADER_RESPONSE_DENYLIST) == []


def test_create_market_analysis_response_rejects_trader_private_leak():
    req = sanitize_market_analysis_request(valid_request_dict())
    leaking_intent = valid_trade_intent_dict()
    leaking_intent["account_balance"] = "100000"

    with pytest.raises(MarketAnalysisTraderBoundaryViolation) as exc_info:
        create_market_analysis_response(req, leaking_intent)
    assert "account_balance" in str(exc_info.value)
    assert "$.account_balance" in exc_info.value.leaks


def test_create_market_analysis_response_rejects_pnl_in_intent():
    req = sanitize_market_analysis_request(valid_request_dict())
    leaking_intent = valid_trade_intent_dict()
    leaking_intent["market_context"]["realized_pnl"] = "450.0"

    with pytest.raises(MarketAnalysisTraderBoundaryViolation):
        create_market_analysis_response(req, leaking_intent)


# ---------------------------------------------------------------------------
# Tool Boundary Tests ("Sem Write Tools")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("forbidden_tool", list(FORBIDDEN_OPERATIONAL_TOOLS))
def test_validate_tools_rejects_forbidden_operational_tools(forbidden_tool: str):
    tools = ["get_closed_candles", forbidden_tool]
    with pytest.raises(ForbiddenWriteToolError) as exc_info:
        validate_tools_for_market_analysis(tools)
    assert forbidden_tool in str(exc_info.value)


def test_validate_tools_rejects_tools_with_mutation_keywords():
    tools = ["cancel_pending_orders", "read_market_depth"]
    with pytest.raises(ForbiddenWriteToolError):
        validate_tools_for_market_analysis(tools)

    tools2 = ["execute_market_swap"]
    with pytest.raises(ForbiddenWriteToolError):
        validate_tools_for_market_analysis(tools2)


def test_validate_tools_accepts_pure_read_tools():
    safe_tools = [
        "get_closed_candles",
        "get_recent_structure",
        "get_volatility",
        "get_market_context",
    ]
    # Should not raise
    validate_tools_for_market_analysis(safe_tools)


def test_verify_no_write_calls_recorded_detects_violations():
    recorded = ["get_closed_candles", "open_directional", "get_volatility"]
    with pytest.raises(ForbiddenWriteToolError) as exc_info:
        verify_no_write_calls_recorded(recorded)
    assert "open_directional" in str(exc_info.value)

    # Clean recorded calls
    clean_recorded = ["get_closed_candles", "get_volatility"]
    verify_no_write_calls_recorded(clean_recorded)


def test_filter_read_only_tools_strips_write_tools():
    mixed = ["get_closed_candles", "preview_intent", "deploy_hedge", "get_volatility"]
    filtered = filter_read_only_tools(mixed)
    assert filtered == ["get_closed_candles", "get_volatility"]


# ---------------------------------------------------------------------------
# Anti-Loop Guard Tests
# ---------------------------------------------------------------------------

def test_anti_loop_guard_blocks_consecutive_market_analysis():
    prior_resp = MarketAnalysisResponseV1(
        schema=RESPONSE_SCHEMA,
        request_id="req-1",
        symbol="BTC-USDT",
        decision_time_ms=1000,
        trade_intent={"decision": "NO_TRADE"},
    )

    # PM waking from market analysis tries to request market analysis again
    with pytest.raises(MarketAnalysisLoopBlockedError):
        check_pm_anti_loop_guard("REQUEST_MARKET_ANALYSIS", prior_market_analysis=prior_resp)

    # Different actions are allowed
    check_pm_anti_loop_guard("HOLD", prior_market_analysis=prior_resp)
    check_pm_anti_loop_guard("REDUCE", prior_market_analysis=prior_resp)
    check_pm_anti_loop_guard("HEDGE", prior_market_analysis=prior_resp)

    # Initial PM wake without prior analysis can request market analysis
    check_pm_anti_loop_guard("REQUEST_MARKET_ANALYSIS", prior_market_analysis=None)


# ---------------------------------------------------------------------------
# Handshake Orchestration Tests (Sync & Async)
# ---------------------------------------------------------------------------

def test_execute_fresh_market_analysis_handshake_sync():
    recorded_calls: list[str] = []

    def mock_trader(request: MarketAnalysisRequestV1) -> dict[str, Any]:
        assert request.symbol == "BTC-USDT"
        recorded_calls.append("get_closed_candles")
        return valid_trade_intent_dict()

    safe_tools = ["get_closed_candles", "get_volatility"]
    resp = execute_fresh_market_analysis_handshake(
        request=valid_request_dict(),
        trader_runner=mock_trader,
        tools=safe_tools,
        recorded_calls_provider=lambda: recorded_calls,
    )

    assert resp.schema == RESPONSE_SCHEMA
    assert resp.symbol == "BTC-USDT"
    assert resp.trade_intent["role"] == "TRADER"


def test_execute_handshake_fails_if_forbidden_tool_used():
    recorded_calls: list[str] = []

    def rogue_trader(request: MarketAnalysisRequestV1) -> dict[str, Any]:
        recorded_calls.append("flatten_everything")
        return valid_trade_intent_dict()

    with pytest.raises(ForbiddenWriteToolError):
        execute_fresh_market_analysis_handshake(
            request=valid_request_dict(),
            trader_runner=rogue_trader,
            recorded_calls_provider=lambda: recorded_calls,
        )


@pytest.mark.asyncio
async def test_async_execute_fresh_market_analysis_handshake():
    recorded_calls: list[str] = []

    async def async_mock_trader(request: MarketAnalysisRequestV1) -> dict[str, Any]:
        assert request.symbol == "BTC-USDT"
        recorded_calls.append("get_recent_structure")
        return valid_trade_intent_dict()

    resp = await async_execute_fresh_market_analysis_handshake(
        request=valid_request_dict(),
        trader_runner=async_mock_trader,
        tools=["get_recent_structure"],
        recorded_calls_provider=lambda: recorded_calls,
    )

    assert resp.schema == RESPONSE_SCHEMA
    assert resp.trade_intent["decision"] == "NO_TRADE"


# ---------------------------------------------------------------------------
# Additional Adversarial & Boundary Safety Tests
# ---------------------------------------------------------------------------

def test_handshake_rejects_leaking_request_before_running_trader():
    trader_called = False

    def runner(_: MarketAnalysisRequestV1) -> dict[str, Any]:
        nonlocal trader_called
        trader_called = True
        return valid_trade_intent_dict()

    leaking_req = valid_request_dict()
    leaking_req["entry_price"] = "90000.0"

    with pytest.raises(MarketAnalysisLeakError):
        execute_fresh_market_analysis_handshake(
            request=leaking_req,
            trader_runner=runner,
        )

    assert not trader_called, "Trader runner must NEVER be called when request leaks private state"


def test_handshake_rejects_write_tools_before_running_trader():
    trader_called = False

    def runner(_: MarketAnalysisRequestV1) -> dict[str, Any]:
        nonlocal trader_called
        trader_called = True
        return valid_trade_intent_dict()

    with pytest.raises(ForbiddenWriteToolError):
        execute_fresh_market_analysis_handshake(
            request=valid_request_dict(),
            trader_runner=runner,
            tools=["create_order"],
        )

    assert not trader_called, "Trader runner must NEVER be called when forbidden write tools are provided"


def test_handshake_rejects_leaking_trader_intent():
    def leaking_trader(_: MarketAnalysisRequestV1) -> dict[str, Any]:
        intent = valid_trade_intent_dict()
        intent["position_id"] = "pos-adversarial-123"
        return intent

    with pytest.raises(MarketAnalysisTraderBoundaryViolation):
        execute_fresh_market_analysis_handshake(
            request=valid_request_dict(),
            trader_runner=leaking_trader,
        )


def test_dataclass_and_pydantic_leak_inspection():
    from dataclasses import dataclass
    from pydantic import BaseModel

    @dataclass
    class LeakingDataClass:
        symbol: str
        unrealized_pnl: str

    obj1 = LeakingDataClass("BTC-USDT", "+500")
    leaks1 = find_private_state_leaks(obj1)
    assert "$.unrealized_pnl" in leaks1

    class LeakingModel(BaseModel):
        symbol: str
        account_id: str

    obj2 = LeakingModel(symbol="BTC-USDT", account_id="acc-1")
    leaks2 = find_private_state_leaks(obj2)
    assert "$.account_id" in leaks2


def test_callable_tools_inspection():
    class ToolWithAttribute:
        name = "execute_prepared_intent"

    def normal_function_tool():
        pass

    def cancel_order():
        pass

    with pytest.raises(ForbiddenWriteToolError):
        validate_tools_for_market_analysis([ToolWithAttribute()])

    with pytest.raises(ForbiddenWriteToolError):
        validate_tools_for_market_analysis([cancel_order])

    # Safe tool callable
    validate_tools_for_market_analysis([normal_function_tool])


def test_pydantic_model_wire_compatibility():
    req = sanitize_market_analysis_request(valid_request_dict())
    serialized = req.model_dump()
    assert serialized["schema"] == "brooks.market-analysis-request.v1"
    assert serialized["request_id"] == "req-brooks-101"
    assert serialized["symbol"] == "BTC-USDT"
    assert serialized["decision_time_ms"] == 1700000000000
    assert serialized["timeframes"] == ["15m", "1h", "4h"]
    assert serialized["market_fields"] == ["ordered_ohlc", "bar_by_bar", "decision_time"]

    # Re-instantiate from serialized
    req2 = MarketAnalysisRequestV1(**serialized)
    assert req2 == req

