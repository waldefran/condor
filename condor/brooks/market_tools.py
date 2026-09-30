"""Time-bounded, read-only Brooks market tools.

The data source is injected by the host. These classes expose no exchange client,
account mutation, or raw provider payload to a Trader or HTF Analyst.
"""

from __future__ import annotations

import inspect
import json
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from condor.brooks.contracts import (
    MarketContextV1,
    PositionManagementInputV2,
    TradeIntentV2,
)

INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
TIMEFRAME_ALIASES = {"M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}
TRADER_MAX_CANDLES = 120
PM_MAX_CANDLES = 30
BROOKS_REFERENCE_MAX_CHARS = 12_000
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?\Z")

_BROOKS_SKILL_ROOT = (
    Path(__file__).resolve().parents[2]
    / "agents"
    / "brooks_price_action"
    / "skills"
)

# Public resource IDs are stable. The model never supplies a path; every ID
# maps to one reviewed file under the packaged Brooks skills directory.
BROOKS_REFERENCE_REGISTRY: dict[str, Path] = {
    "market_context.context_evidence": Path(
        "brooks-market-context/references/context-evidence.md"
    ),
    "market_context.source_notes": Path(
        "brooks-market-context/references/source-notes.md"
    ),
    "trade_entry.entry_evidence": Path(
        "brooks-trade-entry/references/entry-evidence.md"
    ),
    "trade_entry.source_notes": Path(
        "brooks-trade-entry/references/source-notes.md"
    ),
    "position_management.management_evidence": Path(
        "brooks-position-management/references/management-evidence.md"
    ),
}
_BROOKS_REFERENCE_ROLE_ALLOWLIST: dict[str, frozenset[str]] = {
    "TRADER": frozenset(
        {
            "market_context.context_evidence",
            "market_context.source_notes",
            "trade_entry.entry_evidence",
            "trade_entry.source_notes",
        }
    ),
    "CONTEXT_ANALYST": frozenset(
        {"market_context.context_evidence", "market_context.source_notes"}
    ),
    # Kept during the old HTF role transition; it has the same market-only
    # permissions as the generic context analyst.
    "HTF_ANALYST": frozenset(
        {"market_context.context_evidence", "market_context.source_notes"}
    ),
    "POSITION_MANAGER": frozenset(
        {"position_management.management_evidence"}
    ),
}


def make_brooks_reference_tool(
    role: str, *, max_chars: int = BROOKS_REFERENCE_MAX_CHARS
) -> Callable[[str], dict[str, Any]]:
    """Build a read-only reference tool scoped to one Brooks role.

    The callable accepts a registry key, never a path. Its hard output bound
    remains 12,000 characters even if a caller supplies a larger value.
    """
    if role not in _BROOKS_REFERENCE_ROLE_ALLOWLIST:
        raise ValueError(f"unknown Brooks role for references: {role}")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= BROOKS_REFERENCE_MAX_CHARS
    ):
        raise ValueError("reference max_chars must be between 1 and 12000")

    def read_brooks_reference(resource: str) -> dict[str, Any]:
        if not isinstance(resource, str) or resource not in BROOKS_REFERENCE_REGISTRY:
            raise ValueError("unknown Brooks reference resource")
        if resource not in _BROOKS_REFERENCE_ROLE_ALLOWLIST[role]:
            raise PermissionError(f"reference is unavailable to {role}")
        relative = BROOKS_REFERENCE_REGISTRY[resource]
        if relative.is_absolute() or ".." in relative.parts:
            # Treat a bad registry entry as an internal security failure.
            raise PermissionError("invalid Brooks reference registry entry")
        root = _BROOKS_SKILL_ROOT.resolve()
        path = (root / relative).resolve(strict=True)
        if path == root or root not in path.parents or not path.is_file():
            raise PermissionError("Brooks reference escaped its registry root")
        content = path.read_text(encoding="utf-8")
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        return {
            "resource": resource,
            "content": content,
            "truncated": truncated,
        }

    return read_brooks_reference


class ClosedBarError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MarketBar(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    open_time_ms: StrictInt = Field(ge=0)
    close_time_ms: StrictInt = Field(ge=0)
    open: StrictStr
    high: StrictStr
    low: StrictStr
    close: StrictStr
    volume: StrictStr | None = None


def _timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean timestamp")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdecimal():
        result = int(value)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp requires timezone")
        result = int(parsed.timestamp() * 1000)
    else:
        raise ValueError("invalid timestamp")
    if result < 0:
        raise ValueError("negative timestamp")
    return result


def _price(value: Any, *, positive: bool = True) -> tuple[str, Decimal]:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid price")
    result = str(value)
    if not _DECIMAL_RE.fullmatch(result):
        raise ValueError("noncanonical or nonfinite price")
    number = Decimal(result)
    if positive and number <= 0:
        raise ValueError("price must be positive")
    if not positive and number < 0:
        raise ValueError("volume must be nonnegative")
    return result, number


class ClosedBarGate:
    """Port of the harness gate's explicit close, order, gap and OHLC rules."""

    def __init__(self, *, timeframe: str, decision_time_ms: int):
        self.timeframe = TIMEFRAME_ALIASES.get(timeframe, timeframe)
        if self.timeframe not in INTERVAL_MS:
            raise ClosedBarError("UNSUPPORTED_TIMEFRAME", timeframe)
        if isinstance(decision_time_ms, bool) or not isinstance(decision_time_ms, int) or decision_time_ms < 0:
            raise ClosedBarError("INVALID_DECISION_TIME", "decision_time_ms must be nonnegative integer")
        self.decision_time_ms = decision_time_ms

    def validate(self, raw_bars: Sequence[Mapping[str, Any]], *, required_count: int, trigger_timeframe: bool = False) -> list[MarketBar]:
        if isinstance(required_count, bool) or not isinstance(required_count, int) or required_count < 1:
            raise ClosedBarError("INVALID_LIMIT", "required_count must be positive integer")
        if not isinstance(raw_bars, (list, tuple)) or not raw_bars:
            raise ClosedBarError("INSUFFICIENT_HISTORY", "empty bar list")
        interval = INTERVAL_MS[self.timeframe]
        bars: list[MarketBar] = []
        for index, raw in enumerate(raw_bars):
            if not isinstance(raw, Mapping):
                raise ClosedBarError("TRADER_INPUT_INVALID", f"bar {index} is not an object")
            if raw.get("closed") is not True:
                raise ClosedBarError("FORMING_BAR_DETECTED", f"bar {index} lacks closed=true")
            try:
                # No fallback to ts_init or ts_event: those can refer to different events.
                open_ms = _timestamp(raw["open_time_ms"])
                close_ms = _timestamp(raw["close_time_ms"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ClosedBarError("BAR_ORDER_INVALID", f"bar {index} invalid explicit timestamps") from exc
            if close_ms > self.decision_time_ms:
                continue
            if close_ms != open_ms + interval - 1:
                raise ClosedBarError("FORMING_BAR_DETECTED", f"bar {index} has incorrect interval close")
            try:
                open_text, open_price = _price(raw["open"])
                high_text, high = _price(raw["high"])
                low_text, low = _price(raw["low"])
                close_text, close_price = _price(raw["close"])
                volume = _price(raw["volume"], positive=False)[0] if raw.get("volume") is not None else None
            except (KeyError, TypeError, ValueError) as exc:
                raise ClosedBarError("TRADER_INPUT_INVALID", f"bar {index} has invalid price or volume") from exc
            if high < max(open_price, low, close_price) or low > min(open_price, high, close_price):
                raise ClosedBarError("TRADER_INPUT_INVALID", f"bar {index} has incoherent OHLC")
            bar = MarketBar(open_time_ms=open_ms, close_time_ms=close_ms, open=open_text, high=high_text, low=low_text, close=close_text, volume=volume)
            if bars:
                previous = bars[-1]
                if bar.open_time_ms <= previous.open_time_ms or bar.close_time_ms <= previous.close_time_ms:
                    raise ClosedBarError("BAR_ORDER_INVALID", f"bar {index} is duplicated or out of order")
                if bar.open_time_ms != previous.open_time_ms + interval:
                    raise ClosedBarError("BAR_GAP", f"gap before bar {index}")
            bars.append(bar)
        if len(bars) < required_count:
            raise ClosedBarError("INSUFFICIENT_HISTORY", f"{len(bars)} closed bars; require {required_count}")
        window = bars[-required_count:]
        if trigger_timeframe and window[-1].close_time_ms != self.decision_time_ms:
            raise ClosedBarError("BAR_ORDER_INVALID", "trigger bar must close at decision time")
        return window


class CandleSource(Protocol):
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> Sequence[Mapping[str, Any]]: ...


class _MarketTools:
    def __init__(self, *, source: CandleSource, decision_time_ms: int, max_candles: int, market_context: MarketContextV1 | None = None):
        # Resolve statically first so mocks with auto-created attributes remain
        # ordinary three-argument sources unless they explicitly implement this.
        binder = inspect.getattr_static(source, "at_decision_time", None)
        if binder is not None:
            bind = getattr(source, "at_decision_time", None)
            if callable(bind):
                source = bind(decision_time_ms)
        self._source = source
        self._decision_time_ms = decision_time_ms
        self._max_candles = max_candles
        self._market_context = market_context

    async def _candles(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self._max_candles:
            raise ValueError(f"limit must be between 1 and {self._max_candles}")
        gate = ClosedBarGate(timeframe=timeframe, decision_time_ms=self._decision_time_ms)
        # One extra slot permits a provider that includes a future, explicitly closed bar.
        raw = await self._source.fetch_candles(symbol, gate.timeframe, limit + 1)
        return [bar.model_dump(exclude_none=True) for bar in gate.validate(raw, required_count=limit)]

    def get_market_context(self, symbol: str) -> dict[str, Any] | None:
        context = self._market_context
        if context is None:
            return None
        if context.symbol != symbol or context.decision_time_ms > self._decision_time_ms:
            raise ValueError("market context symbol or time mismatch")
        return context.model_dump()

    async def get_recent_structure(self, symbol: str, timeframe: str, *, window: int = 20) -> dict[str, Any]:
        bars = await self._candles(symbol, timeframe, window)
        return {
            "symbol": symbol, "timeframe": TIMEFRAME_ALIASES.get(timeframe, timeframe),
            "as_of_ms": bars[-1]["close_time_ms"], "bars_used": len(bars),
            "range_high": str(max(Decimal(bar["high"]) for bar in bars)),
            "range_low": str(min(Decimal(bar["low"]) for bar in bars)),
            "last_close": bars[-1]["close"],
        }

    async def get_volatility(self, symbol: str, timeframe: str, *, window: int = 20) -> dict[str, Any]:
        bars = await self._candles(symbol, timeframe, window)
        mean_range = sum(Decimal(bar["high"]) - Decimal(bar["low"]) for bar in bars) / Decimal(len(bars))
        return {
            "symbol": symbol, "timeframe": TIMEFRAME_ALIASES.get(timeframe, timeframe),
            "as_of_ms": bars[-1]["close_time_ms"], "bars_used": len(bars),
            "mean_high_low_range": str(mean_range),
        }


class TraderMarketTools(_MarketTools):
    def __init__(self, *, source: CandleSource, decision_time_ms: int, market_context: MarketContextV1 | None = None):
        super().__init__(source=source, decision_time_ms=decision_time_ms, max_candles=TRADER_MAX_CANDLES, market_context=market_context)

    async def get_closed_candles(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        return await self._candles(symbol, timeframe, limit)


class HTFMarketTools(TraderMarketTools):
    """HTF receives only the same market-only read surface as Trader."""


class PMMarketTools(_MarketTools):
    def __init__(self, *, source: CandleSource, snapshot: PositionManagementInputV2, symbol: str, executor_state: Mapping[str, Any] | None = None):
        context = snapshot.latest_market_context
        super().__init__(source=source, decision_time_ms=snapshot.decision_time_ms, max_candles=PM_MAX_CANDLES, market_context=context)
        if not symbol.strip() or any(position.symbol != symbol for position in snapshot.positions):
            raise ValueError("PM snapshot symbol mismatch")
        if any(position.as_of_ms > snapshot.decision_time_ms for position in snapshot.positions):
            raise ValueError("future position state")
        if any(order.as_of_ms > snapshot.decision_time_ms or order.symbol != symbol for order in snapshot.open_orders):
            raise ValueError("future or cross-symbol order state")
        if any(fill.filled_at_ms > snapshot.decision_time_ms or fill.symbol != symbol for fill in snapshot.fills_since_last_event):
            raise ValueError("future or cross-symbol fill state")
        for intent in (snapshot.original_trade_intent, snapshot.latest_trader_intent):
            if intent and (intent.symbol != symbol or intent.decision_time_ms > snapshot.decision_time_ms):
                raise ValueError("future or cross-symbol trade intent")
        if context and (context.symbol != symbol or context.decision_time_ms > snapshot.decision_time_ms):
            raise ValueError("future or cross-symbol market context")
        try:
            json.dumps(executor_state or {}, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("executor state must be JSON data") from exc
        self._snapshot = snapshot
        self._symbol = symbol
        self._executor_state = deepcopy(dict(executor_state or {}))

    def _check_symbol(self, symbol: str) -> None:
        if symbol != self._symbol:
            raise ValueError("PM symbol mismatch")

    async def get_candles(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        self._check_symbol(symbol)
        return await self._candles(symbol, timeframe, limit)

    def get_position_state(self) -> list[dict[str, Any]]:
        return [position.model_dump() for position in self._snapshot.positions]

    def get_executor_state(self) -> dict[str, Any]:
        return deepcopy(self._executor_state)

    def get_open_orders(self) -> list[dict[str, Any]]:
        return [order.model_dump() for order in self._snapshot.open_orders]

    def get_recent_fills(self) -> list[dict[str, Any]]:
        return [fill.model_dump() for fill in self._snapshot.fills_since_last_event]

    def get_original_trade_intent(self) -> dict[str, Any] | None:
        intent: TradeIntentV2 | None = self._snapshot.original_trade_intent
        return intent.model_dump() if intent else None

    def get_latest_trader_intent(self) -> dict[str, Any] | None:
        intent: TradeIntentV2 | None = self._snapshot.latest_trader_intent
        return intent.model_dump() if intent else None

    def get_market_context(self, symbol: str) -> dict[str, Any] | None:
        self._check_symbol(symbol)
        return super().get_market_context(symbol)

    async def get_recent_structure(self, symbol: str, timeframe: str, *, window: int = 20) -> dict[str, Any]:
        self._check_symbol(symbol)
        return await super().get_recent_structure(symbol, timeframe, window=window)

    async def get_volatility(self, symbol: str, timeframe: str, *, window: int = 20) -> dict[str, Any]:
        self._check_symbol(symbol)
        return await super().get_volatility(symbol, timeframe, window=window)
