"""Strict market-only privacy boundary and handshake for Brooks REQUEST_MARKET_ANALYSIS.

Follows the fail-closed market boundary from brooks-harness/src/pm/market-analysis-boundary.ts.
The Position Manager (PM) can request a fresh market analysis at decision time T0.
The request is strictly market-only and must never leak private account or position state.
The analyst/trader operates in a read-only sandbox with zero write tools (preview=false, execution=false).
Results return as an independent trade intent wrapped into MarketAnalysisResponseV1.
Anti-loop guard ensures PM cannot chain back-to-back REQUEST_MARKET_ANALYSIS calls.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema" .* shadows an attribute in parent .*',
    category=UserWarning,
)

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

# ---------------------------------------------------------------------------
# Schemas and Constants
# ---------------------------------------------------------------------------

REQUEST_SCHEMA = "brooks.market-analysis-request.v1"
RESPONSE_SCHEMA = "brooks.market-analysis-response.v1"

MarketAnalysisField = Literal["ordered_ohlc", "bar_by_bar", "decision_time"]
ALLOWED_MARKET_FIELDS: frozenset[str] = frozenset({"ordered_ohlc", "bar_by_bar", "decision_time"})
DEFAULT_ALLOWED_TIMEFRAMES: frozenset[str] = frozenset({
    "15m", "1h", "4h", "1d",
    "M15", "H1", "H4", "D1",
})

# Private state denylist matching brooks-harness and Section 24 of the Brooks plan
PRIVATE_STATE_DENYLIST: frozenset[str] = frozenset({
    "account",
    "action",
    "available_balance",
    "available_margin",
    "balance",
    "entry",
    "entry_price",
    "entry_prices",
    "equity",
    "execution",
    "fees",
    "fills",
    "final_pnl",
    "funding",
    "future_bars",
    "future_observation",
    "hedge",
    "hedge_plan",
    "hedge_ratio",
    "hedges",
    "intended_action",
    "leverage",
    "loss_streak",
    "mae",
    "management_history",
    "margin",
    "mfe",
    "open_orders",
    "open_positions",
    "order",
    "orders",
    "outcome_bars",
    "pm_intent",
    "pm_opinion",
    "pm_reason",
    "pnl",
    "portfolio",
    "position",
    "position_id",
    "position_ids",
    "position_side",
    "position_size",
    "position_state",
    "positions",
    "profitability",
    "protection_status",
    "qty",
    "quantity",
    "ratio",
    "realized_pnl",
    "realized_return",
    "reason",
    "side",
    "strategy_stop_required",
    "target_hedge_ratio",
    "trade_history",
    "unrealized_pnl",
})

TRADER_RESPONSE_DENYLIST: frozenset[str] = frozenset({
    "account",
    "available_balance",
    "available_margin",
    "balance",
    "equity",
    "fees",
    "fills",
    "funding",
    "hedge",
    "hedge_plan",
    "hedge_ratio",
    "hedges",
    "leverage",
    "loss_streak",
    "margin",
    "open_orders",
    "open_positions",
    "order",
    "orders",
    "pnl",
    "portfolio",
    "position",
    "position_id",
    "position_ids",
    "position_side",
    "position_size",
    "position_state",
    "positions",
    "realized_pnl",
    "unrealized_pnl",
})

FORBIDDEN_OPERATIONAL_TOOLS: frozenset[str] = frozenset({
    "preview_intent",
    "execute_prepared_intent",
    "open_directional",
    "add_to_position",
    "close_directional",
    "deploy_hedge",
    "flatten_everything",
    "create_order",
    "cancel_order",
    "cancel_all_orders",
    "replace_order",
    "place_order",
    "submit_order",
    "execute_order",
    "open_position",
    "close_position",
    "modify_order",
    "set_leverage",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MarketAnalysisBoundaryError(ValueError):
    """Base class for all market analysis boundary violations."""


class MarketAnalysisLeakError(MarketAnalysisBoundaryError):
    """Raised when private account or position state leaks into market analysis."""

    def __init__(self, message: str, leaks: list[str]) -> None:
        super().__init__(message)
        self.leaks = list(leaks)


class MarketAnalysisInvalidRequestError(MarketAnalysisBoundaryError):
    """Raised when a market analysis request structure or field is invalid."""


class MarketAnalysisUnsupportedTimeframeError(MarketAnalysisInvalidRequestError):
    """Raised when an unapproved timeframe is requested."""


class MarketAnalysisTraderBoundaryViolation(MarketAnalysisBoundaryError):
    """Raised when trader analysis output contains private state fields."""

    def __init__(self, message: str, leaks: list[str]) -> None:
        super().__init__(message)
        self.leaks = list(leaks)


class ForbiddenWriteToolError(MarketAnalysisBoundaryError):
    """Raised when write/mutation or operational tools are supplied or executed in market analysis."""


class MarketAnalysisLoopBlockedError(MarketAnalysisBoundaryError):
    """Raised when PM attempts back-to-back REQUEST_MARKET_ANALYSIS in an infinite loop."""


# ---------------------------------------------------------------------------
# Leak Detection
# ---------------------------------------------------------------------------

def normalize_key(key: Any) -> tuple[str, list[str]]:
    """Normalize a key from camelCase/PascalCase to snake_case and extract tokens."""
    raw = str(key)
    # Convert camelCase / PascalCase to snake_case
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
    snake = re.sub(r"[_\-.\s]+", "_", s2)
    tokens = [t for t in snake.split("_") if t]
    return snake, tokens


def _to_inspectable(val: Any) -> Any:
    """Convert Pydantic models or dataclasses into plain dicts for recursive inspection."""
    if hasattr(val, "model_dump"):
        return val.model_dump(mode="python")
    if dataclasses.is_dataclass(val) and not isinstance(val, type):
        return dataclasses.asdict(val)
    return val


def find_private_state_leaks(
    obj: Any,
    denylist: frozenset[str] = PRIVATE_STATE_DENYLIST,
    path: str = "$",
) -> list[str]:
    """Recursively inspect an object for private state leaks.

    Scans mappings, lists, dataclasses, Pydantic models, and embedded JSON strings.
    Matches normalized keys and sub-key tokens against the denylist.
    """
    found: list[str] = []
    plain = _to_inspectable(obj)

    if isinstance(plain, Mapping):
        for key, value in plain.items():
            normalized, tokens = normalize_key(key)
            if normalized in denylist or any(t in denylist for t in tokens):
                found.append(f"{path}.{key}")
            found.extend(find_private_state_leaks(value, denylist, f"{path}.{key}"))
    elif isinstance(plain, (list, tuple, set)):
        for idx, item in enumerate(plain):
            found.extend(find_private_state_leaks(item, denylist, f"{path}[{idx}]"))
    elif isinstance(plain, str):
        stripped = plain.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                parsed = json.loads(stripped)
                found.extend(find_private_state_leaks(parsed, denylist, f"{path}.(json)"))
            except Exception:
                pass

    return found


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class MarketAnalysisRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())

    schema: Literal["brooks.market-analysis-request.v1"] = REQUEST_SCHEMA
    request_id: StrictStr = Field(min_length=1)
    symbol: StrictStr = Field(min_length=1)
    decision_time_ms: StrictInt = Field(ge=0)
    timeframes: list[StrictStr] = Field(min_length=1)
    market_fields: list[Literal["ordered_ohlc", "bar_by_bar", "decision_time"]] = Field(min_length=1)

    @field_validator("request_id", "symbol")
    @classmethod
    def nonempty_text(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("must not be empty or whitespace only")
        return trimmed

    @field_validator("timeframes")
    @classmethod
    def valid_timeframes_list(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("timeframes must not be empty")
        cleaned = [item.strip() for item in v]
        if any(not item for item in cleaned):
            raise ValueError("timeframe item must not be empty")
        return cleaned


class MarketAnalysisResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False, protected_namespaces=())

    schema: Literal["brooks.market-analysis-response.v1"] = RESPONSE_SCHEMA
    request_id: StrictStr = Field(min_length=1)
    symbol: StrictStr = Field(min_length=1)
    decision_time_ms: StrictInt = Field(ge=0)
    trade_intent: Any

    @field_validator("request_id", "symbol")
    @classmethod
    def nonempty_text(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("must not be empty or whitespace only")
        return trimmed


# ---------------------------------------------------------------------------
# Request Sanitization & Boundary Validation
# ---------------------------------------------------------------------------

def sanitize_market_analysis_request(
    raw_request: Any,
    allowed_timeframes: Sequence[str] | set[str] | frozenset[str] | None = None,
) -> MarketAnalysisRequestV1:
    """Validate, scrub, and reconstruct a market analysis request.

    Guarantees:
    1. Zero private leaks in the incoming raw request (fails closed if any detected).
    2. Exact field reconstruction: drops any unapproved extra fields.
    3. Strict validation of request_id, symbol, decision_time_ms, timeframes, and market_fields.
    4. Enforces policy allowed_timeframes.
    5. Asserts zero private leaks in the final reconstructed request.
    """
    if raw_request is None:
        raise MarketAnalysisInvalidRequestError("market_analysis_request must not be None")

    inspectable = _to_inspectable(raw_request)
    if not isinstance(inspectable, Mapping):
        raise MarketAnalysisInvalidRequestError("market_analysis_request must be an object/mapping")

    # 1. Recursive private-state leak scan
    leaks = find_private_state_leaks(inspectable, PRIVATE_STATE_DENYLIST)
    if leaks:
        raise MarketAnalysisLeakError(
            f"Private state leak detected in market_analysis_request: {', '.join(leaks)}",
            leaks=leaks,
        )

    # 2. Extract and validate required fields
    request_id = inspectable.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise MarketAnalysisInvalidRequestError("market_analysis_request.request_id must be a non-empty string")

    symbol = inspectable.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise MarketAnalysisInvalidRequestError("market_analysis_request.symbol must be a non-empty string")

    decision_time_ms = inspectable.get("decision_time_ms")
    if not isinstance(decision_time_ms, int) or isinstance(decision_time_ms, bool) or decision_time_ms < 0:
        raise MarketAnalysisInvalidRequestError("market_analysis_request.decision_time_ms must be a non-negative integer")

    raw_timeframes = inspectable.get("timeframes")
    if not isinstance(raw_timeframes, (list, tuple)) or len(raw_timeframes) == 0:
        raise MarketAnalysisInvalidRequestError("market_analysis_request.timeframes must be a non-empty array")

    cleaned_timeframes: list[str] = []
    for tf in raw_timeframes:
        if not isinstance(tf, str) or not tf.strip():
            raise MarketAnalysisInvalidRequestError("timeframe elements must be non-empty strings")
        cleaned_timeframes.append(tf.strip())

    allowed_tf_set = frozenset(allowed_timeframes) if allowed_timeframes is not None else DEFAULT_ALLOWED_TIMEFRAMES
    for tf in cleaned_timeframes:
        if tf not in allowed_tf_set:
            allowed_list = sorted(allowed_tf_set)
            raise MarketAnalysisUnsupportedTimeframeError(
                f"Timeframe '{tf}' requested by PM is not allowed by policy (allowed: {allowed_list})"
            )

    raw_market_fields = inspectable.get("market_fields")
    if not isinstance(raw_market_fields, (list, tuple)) or len(raw_market_fields) == 0:
        raise MarketAnalysisInvalidRequestError("market_analysis_request.market_fields must be a non-empty array")

    cleaned_market_fields: list[str] = []
    for mf in raw_market_fields:
        if not isinstance(mf, str) or not mf.strip():
            raise MarketAnalysisInvalidRequestError("market_fields elements must be non-empty strings")
        cleaned_mf = mf.strip()
        if cleaned_mf not in ALLOWED_MARKET_FIELDS:
            raise MarketAnalysisInvalidRequestError(
                f"Market field '{cleaned_mf}' is invalid (allowed: {sorted(ALLOWED_MARKET_FIELDS)})"
            )
        cleaned_market_fields.append(cleaned_mf)

    # 3. Exact field reconstruction: pure sanitized object without raw garbage
    clean_request = MarketAnalysisRequestV1(
        schema=REQUEST_SCHEMA,
        request_id=request_id.strip(),
        symbol=symbol.strip(),
        decision_time_ms=decision_time_ms,
        timeframes=cleaned_timeframes,
        market_fields=cleaned_market_fields,  # type: ignore[arg-type]
    )

    # 4. Assert zero private leaks in final sanitized object
    final_leaks = find_private_state_leaks(clean_request.model_dump(), PRIVATE_STATE_DENYLIST)
    if final_leaks:
        raise MarketAnalysisLeakError(
            f"Sanitized market_analysis_request leaked private keys: {', '.join(final_leaks)}",
            leaks=final_leaks,
        )

    return clean_request


# ---------------------------------------------------------------------------
# Response Creation & Validation
# ---------------------------------------------------------------------------

def create_market_analysis_response(
    request: MarketAnalysisRequestV1 | Mapping[str, Any],
    trade_intent: Any,
    denylist: frozenset[str] = TRADER_RESPONSE_DENYLIST,
) -> MarketAnalysisResponseV1:
    """Wrap a fresh TradeIntent into MarketAnalysisResponseV1 after strict boundary checks.

    Guarantees:
    1. TradeIntent contains zero private state fields.
    2. Response binds to the matching request_id, symbol, and decision_time_ms.
    3. Final response is strictly validated.
    """
    if trade_intent is None:
        raise MarketAnalysisBoundaryError("trade_intent must not be None")

    inspectable_intent = _to_inspectable(trade_intent)

    # 1. Recursive leak scan on trade intent
    leaks = find_private_state_leaks(inspectable_intent, denylist)
    if leaks:
        raise MarketAnalysisTraderBoundaryViolation(
            f"TradeIntent contains private state fields: {', '.join(leaks)}",
            leaks=leaks,
        )

    # 2. Extract request attributes
    if isinstance(request, MarketAnalysisRequestV1):
        req_id = request.request_id
        symbol = request.symbol
        decision_time_ms = request.decision_time_ms
    elif isinstance(request, Mapping):
        req_id = str(request["request_id"]).strip()
        symbol = str(request["symbol"]).strip()
        decision_time_ms = int(request["decision_time_ms"])
    else:
        raise MarketAnalysisBoundaryError("request must be MarketAnalysisRequestV1 or Mapping")

    # 3. Construct response
    response = MarketAnalysisResponseV1(
        schema=RESPONSE_SCHEMA,
        request_id=req_id,
        symbol=symbol,
        decision_time_ms=decision_time_ms,
        trade_intent=inspectable_intent,
    )

    # 4. Assert zero private leaks in response
    resp_leaks = find_private_state_leaks(response.model_dump(), denylist)
    if resp_leaks:
        raise MarketAnalysisTraderBoundaryViolation(
            f"MarketAnalysisResponse leaked private keys: {', '.join(resp_leaks)}",
            leaks=resp_leaks,
        )

    return response


# ---------------------------------------------------------------------------
# Tool Boundary Verification ("Sem Write Tools")
# ---------------------------------------------------------------------------

def _get_tool_name(tool: Any) -> str:
    """Extract a canonical tool name from a callable, object, or string."""
    if isinstance(tool, str):
        return tool.strip()
    if hasattr(tool, "name") and isinstance(tool.name, str):
        return tool.name.strip()
    if hasattr(tool, "__name__"):
        return str(tool.__name__).strip()
    return str(tool).strip()


def validate_tools_for_market_analysis(
    tools: Sequence[Any],
    forbidden: frozenset[str] = FORBIDDEN_OPERATIONAL_TOOLS,
) -> None:
    """Verify that a set of tools passed to the market analyst contains ZERO write/operational tools.

    Fails closed if any tool name matches forbidden operational tools or write keywords.
    """
    for tool in tools:
        name = _get_tool_name(tool)
        normalized, tokens = normalize_key(name)
        if name in forbidden or normalized in forbidden:
            raise ForbiddenWriteToolError(
                f"Forbidden operational/write tool '{name}' provided for market analysis"
            )
        # Extra safeguard: reject tools with mutation keywords
        mutation_keywords = {"execute", "cancel", "replace", "preview", "hedge", "deploy", "submit", "flatten"}
        if any(kw in tokens for kw in mutation_keywords):
            raise ForbiddenWriteToolError(
                f"Tool '{name}' contains mutation keyword and is forbidden for market analysis"
            )


def verify_no_write_calls_recorded(
    recorded_calls: Sequence[Any],
    forbidden: frozenset[str] = FORBIDDEN_OPERATIONAL_TOOLS,
) -> None:
    """Machine-verify that zero operational/write tools were called during market analysis."""
    offending: list[str] = []
    for call in recorded_calls:
        name = _get_tool_name(call)
        normalized, _ = normalize_key(name)
        if name in forbidden or normalized in forbidden:
            offending.append(name)

    if offending:
        raise ForbiddenWriteToolError(
            f"Forbidden operational tool calls were recorded during market analysis: {', '.join(offending)}"
        )


def filter_read_only_tools(
    tools: Sequence[Any],
    forbidden: frozenset[str] = FORBIDDEN_OPERATIONAL_TOOLS,
) -> list[Any]:
    """Filter an incoming tool sequence, retaining only tools safe for read-only market analysis."""
    safe_tools: list[Any] = []
    for tool in tools:
        name = _get_tool_name(tool)
        normalized, tokens = normalize_key(name)
        if name in forbidden or normalized in forbidden:
            continue
        mutation_keywords = {"execute", "cancel", "replace", "preview", "hedge", "deploy", "submit", "flatten"}
        if any(kw in tokens for kw in mutation_keywords):
            continue
        safe_tools.append(tool)
    return safe_tools


# ---------------------------------------------------------------------------
# Anti-Loop Guard
# ---------------------------------------------------------------------------

def check_pm_anti_loop_guard(
    decision_action: str,
    prior_market_analysis: Any | None = None,
) -> None:
    """Prevent infinite loop if PM requests market analysis immediately upon receiving market analysis.

    If prior_market_analysis is present (PM woke at T1 from analysis) and action is
    REQUEST_MARKET_ANALYSIS, the loop is strictly blocked.
    """
    if prior_market_analysis is not None and decision_action == "REQUEST_MARKET_ANALYSIS":
        raise MarketAnalysisLoopBlockedError(
            "PM requested REQUEST_MARKET_ANALYSIS immediately after receiving market analysis (loop strictly blocked)"
        )


# ---------------------------------------------------------------------------
# Handshake Orchestration
# ---------------------------------------------------------------------------

def execute_fresh_market_analysis_handshake(
    *,
    request: Any,
    trader_runner: Callable[[MarketAnalysisRequestV1], Any],
    tools: Sequence[Any] | None = None,
    recorded_calls_provider: Callable[[], Sequence[Any]] | None = None,
    allowed_timeframes: Sequence[str] | set[str] | frozenset[str] | None = None,
) -> MarketAnalysisResponseV1:
    """Execute synchronous fresh market analysis handshake with full boundary enforcement."""
    # 1. Enforce tool boundary before invoking analyst
    if tools is not None:
        validate_tools_for_market_analysis(tools)

    # 2. Sanitize and reconstruct request
    clean_request = sanitize_market_analysis_request(
        request, allowed_timeframes=allowed_timeframes
    )

    # 3. Run fresh trader analysis in market-only mode
    trade_intent = trader_runner(clean_request)

    # 4. Verify zero write calls occurred
    if recorded_calls_provider is not None:
        verify_no_write_calls_recorded(recorded_calls_provider())

    # 5. Build and validate response
    return create_market_analysis_response(clean_request, trade_intent)


async def async_execute_fresh_market_analysis_handshake(
    *,
    request: Any,
    trader_runner: Callable[[MarketAnalysisRequestV1], Awaitable[Any] | Any],
    tools: Sequence[Any] | None = None,
    recorded_calls_provider: Callable[[], Sequence[Any]] | None = None,
    allowed_timeframes: Sequence[str] | set[str] | frozenset[str] | None = None,
) -> MarketAnalysisResponseV1:
    """Execute asynchronous fresh market analysis handshake with full boundary enforcement."""
    # 1. Enforce tool boundary
    if tools is not None:
        validate_tools_for_market_analysis(tools)

    # 2. Sanitize and reconstruct request
    clean_request = sanitize_market_analysis_request(
        request, allowed_timeframes=allowed_timeframes
    )

    # 3. Run fresh trader analysis
    result = trader_runner(clean_request)
    trade_intent = await result if inspect.isawaitable(result) else result

    # 4. Verify zero write calls occurred
    if recorded_calls_provider is not None:
        verify_no_write_calls_recorded(recorded_calls_provider())

    # 5. Build and validate response
    return create_market_analysis_response(clean_request, trade_intent)
