"""Independent, read-only Position PM agent.

The PM wakes on position/fill/hedge/timer events (and on trader/analyst
events only while a position is active), investigates via narrow read-only
tools, and returns a management *decision*. Only the deterministic GM may
turn that decision into an exchange write.

Integration seams (owned by other worktrees; wired by the Integrator after
cherry-pick -- this module never imports them at module scope):

- ``condor.brooks.contracts.ManagementDecisionV2``: Pydantic model with at
  least ``action: str``, ``decision_time_ms: int`` and
  ``position_ids: list[str]``; classmethod
  ``model_validate(obj) -> ManagementDecisionV2`` and method
  ``model_dump(mode="json") -> dict``.
- ``condor.brooks.market_tools.ClosedBarGate``: constructed as
  ``ClosedBarGate(timeframe=<tf>, decision_time_ms=<ms>)``; method
  ``validate(raw, required_count=<n>)`` returning bar models exposing
  ``close_time_ms: int`` and ``model_dump(exclude_none=True) -> dict``
  with ``high``/``low``/``close`` decimal strings.
- ``condor.brooks.agent_runner.run_role``: async callable used only when no
  ``runner`` is injected, called with the single canonical convention
  ``run_role("POSITION_MANAGER", prompt=<small dict>,
  output_model=ManagementDecisionV2, market_tools=<name->callable>,
  agent_key=..., timeout_sec=..., max_tool_calls=...,
  user_id=...)``. Tests and the Integrator inject a fake/real runner
  instead (an async callable with the SAME convention, or any object with
  an async ``run`` method taking the same arguments).
- ``condor.brooks.events.BrooksEvent`` / ``EventType``: used only when
  ``publish`` exposes a ``.publish`` method (real EventBus); otherwise the
  plain ``MANAGEMENT_INTENT_CREATED`` envelope dict is passed to the
  ``publish`` callback.
- Store/bus adapters (Injected by the host, never imported here):
  ``load_context(correlation_id)`` -> snapshot mapping (or None);
  ``save_decision(correlation_id, decision)``; ``publish(event)``;
  ``record_market_read(correlation_id, record)`` (called with the accept /
  reject audit record *before* candle data reaches the model);
  ``candle_source(symbol, timeframe, limit)`` -> raw bars (or an object
  with async ``fetch_candles(symbol, timeframe, limit)``);
  ``list_active_correlations(symbol)`` -> iterable of correlation ids for
  global (timer/analyst) wakes.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

PM_WAKE_EVENTS = frozenset(
    {
        "PM_TIMER",
        "POSITION_OPENED",
        "POSITION_CHANGED",
        "POSITION_CLOSED",
        "ORDER_CHANGED",
        "FILL",
        "HEDGE_OPENED",
        "HEDGE_CHANGED",
        "HEDGE_REMOVED",
        "TRADER_INTENT_CREATED",
        "MARKET_CONTEXT_UPDATED",
        "MARKET_ANALYSIS_COMPLETED",
    }
)
ANALYST_EVENTS = frozenset({"TRADER_INTENT_CREATED", "MARKET_CONTEXT_UPDATED"})
MARKET_TIMEFRAMES = frozenset({"15m", "1h", "4h", "1d"})
PM_CANDLE_LIMIT = 30

# ManagementDecisionV2 actions the V1 PM/GM pair may emit. Anything else the
# model returns (unknown, ambiguous, or a deferred V2 action such as PROTECT)
# is coerced to MANAGEMENT_BLOCKED before persistence -- fail closed, never
# a write. Kept here (not imported) so this module stays self-contained
# until the Integrator wires condor/brooks/contracts.py after cherry-pick.
PM_V1_ACTIONS = frozenset(
    {
        "HOLD",
        "REDUCE",
        "CLOSE",
        "HEDGE",
        "INCREASE_HEDGE",
        "REDUCE_HEDGE",
        "REMOVE_HEDGE",
        "REQUEST_MARKET_ANALYSIS",
        "RECONCILE_STATE",
        "MANAGEMENT_BLOCKED",
    }
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError("PM context and events must be mappings or Pydantic models")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class PMReadTools:
    """Per-wake read tools, bound to one symbol and decision time.

    The host supplies only a candle source. Every market read passes through
    ClosedBarGate here, and the decision is recorded before data reaches the
    model. No venue or mutation client is passed to the model.
    """

    def __init__(
        self,
        *,
        context: Mapping[str, Any],
        candle_source: Any,
        record_market_read: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        self.context = _as_dict(context)
        self.symbol = str(self.context["symbol"])
        self.decision_time_ms = int(self.context["decision_time_ms"])
        binder = inspect.getattr_static(candle_source, "at_decision_time", None)
        if binder is not None:
            bind = getattr(candle_source, "at_decision_time", None)
            if callable(bind):
                candle_source = bind(self.decision_time_ms)
        self._candle_source = candle_source
        self._record_market_read = record_market_read
        self.correlation_id = str(self.context["correlation_id"])

    def _symbol(self, symbol: str | None) -> str:
        if symbol is not None and symbol != self.symbol:
            raise ValueError("PM tools are bound to the current position symbol")
        return self.symbol

    @staticmethod
    def _timeframe(timeframe: str) -> str:
        if timeframe not in MARKET_TIMEFRAMES:
            raise ValueError("timeframe is not allowed for PM")
        return timeframe

    async def get_market_context(self) -> Any:
        if "macro_contexts" in self.context:
            return self.context["macro_contexts"]
        return self.context.get("latest_market_context")

    async def get_latest_trader_intent(self) -> Any:
        return self.context.get("latest_trader_intent")

    async def get_original_trade_intent(self) -> Any:
        return self.context.get("original_trade_intent")

    async def get_position_state(self) -> Any:
        return self.context.get("positions", self.context.get("position"))

    async def get_executor_state(self) -> Any:
        return self.context.get("executor", self.context.get("executor_state"))

    async def get_open_orders(self) -> Any:
        return self.context.get("open_orders", [])

    async def get_recent_fills(self) -> Any:
        return self.context.get(
            "recent_fills", self.context.get("fills_since_last_event", [])
        )

    async def get_recent_structure(self, timeframe: str = "1h") -> Any:
        bars = await self.get_candles(self.symbol, timeframe, 20)
        return {
            "symbol": self.symbol,
            "timeframe": timeframe,
            "as_of_ms": bars[-1]["close_time_ms"],
            "bars_used": len(bars),
            "range_high": str(max(Decimal(bar["high"]) for bar in bars)),
            "range_low": str(min(Decimal(bar["low"]) for bar in bars)),
            "last_close": bars[-1]["close"],
        }

    async def get_volatility(self, timeframe: str = "1h", window: int = 14) -> Any:
        if (
            isinstance(window, bool)
            or not isinstance(window, int)
            or not 1 <= window <= PM_CANDLE_LIMIT
        ):
            raise ValueError("volatility window must be between 1 and 30")
        bars = await self.get_candles(self.symbol, timeframe, window)
        mean_range = sum(
            Decimal(bar["high"]) - Decimal(bar["low"]) for bar in bars
        ) / Decimal(len(bars))
        return {
            "symbol": self.symbol,
            "timeframe": timeframe,
            "as_of_ms": bars[-1]["close_time_ms"],
            "bars_used": len(bars),
            "mean_high_low_range": str(mean_range),
        }

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 30) -> Any:
        record = {
            "type": "PM_CANDLE_GATE",
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "decision_time_ms": self.decision_time_ms,
        }
        try:
            self._symbol(symbol)
            self._timeframe(timeframe)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= PM_CANDLE_LIMIT
            ):
                raise ValueError("PM candle limit must be between 1 and 30")
            from condor.brooks.market_tools import ClosedBarGate

            gate = ClosedBarGate(
                timeframe=timeframe, decision_time_ms=self.decision_time_ms
            )
            source = getattr(self._candle_source, "fetch_candles", self._candle_source)
            raw = await _resolve(source(self.symbol, gate.timeframe, limit + 1))
            bars = gate.validate(raw, required_count=limit)
        except Exception as exc:
            record.update(
                {"accepted": False, "reason": getattr(exc, "code", type(exc).__name__)}
            )
            await _resolve(self._record_market_read(self.correlation_id, record))
            raise
        record.update(
            {"accepted": True, "close_times_ms": [bar.close_time_ms for bar in bars]}
        )
        await _resolve(self._record_market_read(self.correlation_id, record))
        return [bar.model_dump(exclude_none=True) for bar in bars]

    def as_tools(self) -> list[Callable[..., Awaitable[Any]]]:
        """Explicit allowlist passed into the agent's actual tool loop."""
        return [
            self.get_market_context,
            self.get_recent_structure,
            self.get_volatility,
            self.get_latest_trader_intent,
            self.get_original_trade_intent,
            self.get_position_state,
            self.get_executor_state,
            self.get_open_orders,
            self.get_recent_fills,
            self.get_candles,
        ]

    def named_tools(self) -> dict[str, Callable[..., Awaitable[Any]]]:
        return {tool.__name__: tool for tool in self.as_tools()}


def _small_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Bound the first prompt; the agent can ask for additional market data."""
    source = _as_dict(context)
    allowed = (
        "correlation_id",
        "symbol",
        "decision_time_ms",
        "account",
        "position",
        "positions",
        "executor",
        "executor_state",
        "open_orders",
        "recent_fills",
        "fills_since_last_event",
        "pnl",
        "costs",
        "original_trade_intent",
        "latest_trader_intent",
        "latest_market_context",
        "macro_contexts",
        "latest_trader_intent_freshness",
        "management_history",
        "management_policy",
        "hedge_state",
        "margin_health",
        "market_analysis",
        "shadow_mode",
    )
    compact = {key: source[key] for key in allowed if key in source}
    for key, limit in (
        ("open_orders", 20),
        ("recent_fills", 10),
        ("fills_since_last_event", 10),
        ("management_history", 10),
    ):
        if isinstance(compact.get(key), list):
            compact[key] = compact[key][-limit:]
    return compact


class PositionManager:
    """Wake on management signals, run the agent, persist its typed decision."""

    def __init__(
        self,
        *,
        runner: Any = None,
        load_context: Callable[[str], Any],
        save_decision: Callable[[str, Any], Any],
        publish: Any,
        candle_source: Any,
        record_market_read: Callable[[str, dict[str, Any]], Any],
        list_active_correlations: Callable[[str | None], Any] | None = None,
        agent_key: str | None = None,
        timeout_sec: float = 60,
        max_tool_calls: int = 8,
        user_id: int | None = None,
    ) -> None:
        self.runner = runner
        if runner is None and not agent_key:
            raise ValueError("agent_key is required for the Brooks role runner")
        self.load_context = load_context
        self.save_decision = save_decision
        self.publish = publish
        self.candle_source = candle_source
        self.record_market_read = record_market_read
        self.list_active_correlations = list_active_correlations
        self.agent_key = agent_key
        self.timeout_sec = timeout_sec
        self.max_tool_calls = max_tool_calls
        self.user_id = user_id

    async def handle_event(self, event: Mapping[str, Any]) -> Any | None:
        envelope = _as_dict(event)
        event_type = str(envelope.get("type", ""))
        if event_type not in PM_WAKE_EVENTS:
            return None
        correlation_id = str(envelope.get("correlation_id") or "")
        if not correlation_id:
            if event_type == "PM_TIMER" or event_type in ANALYST_EVENTS:
                if self.list_active_correlations is None:
                    raise RuntimeError("global PM wake requires active binding lookup")
                symbol = envelope.get("symbol")
                correlations = await _resolve(self.list_active_correlations(symbol))
                decisions = []
                for binding in dict.fromkeys(correlations):
                    decision = await self.handle_event(
                        {**envelope, "correlation_id": binding}
                    )
                    if decision is not None:
                        decisions.append(decision)
                return decisions
            return None
        loaded = await _resolve(self.load_context(correlation_id))
        if loaded is None:
            return None
        context = _as_dict(loaded)
        if not context or context.get("correlation_id") != correlation_id:
            return None
        if envelope.get("symbol") not in (None, "", "*") and envelope[
            "symbol"
        ] != context.get("symbol"):
            return None
        if event_type in ANALYST_EVENTS and not self._has_active_position(context):
            return None
        if not context.get("market_analysis"):
            analysis = envelope.get("payload", {}).get("market_analysis")
            if (
                analysis is None
                and envelope.get("payload", {}).get("schema")
                == "brooks.market-analysis-response.v1"
            ):
                analysis = envelope.get("payload")
            if analysis is not None:
                context["market_analysis"] = analysis
        context.setdefault("decision_time_ms", envelope.get("created_at_ms"))
        if not context.get("decision_time_ms"):
            raise ValueError("PM requires a decision time")
        tools = PMReadTools(
            context=context,
            candle_source=self.candle_source,
            record_market_read=self.record_market_read,
        )
        # The shared runner owns the model/tool-call loop, output validation,
        # model selection and timeout. These tools contain no write primitive.
        from condor.brooks.contracts import ManagementDecisionV2

        runner = self.runner
        if runner is None:
            from condor.brooks.agent_runner import run_role

            runner = run_role
        run = getattr(runner, "run", runner)
        # Single canonical role-runner convention (shared with Trader/HTF):
        # run_role(role, prompt, output_model, market_tools, *, agent_key,
        # timeout_sec, max_tool_calls, user_id). No silent adapters, no dual
        # keyword spellings (tools/context are not accepted).
        decision = await run(
            "POSITION_MANAGER",
            prompt=_small_context(context),
            output_model=ManagementDecisionV2,
            market_tools=tools.named_tools(),
            agent_key=self.agent_key,
            timeout_sec=self.timeout_sec,
            max_tool_calls=self.max_tool_calls,
            user_id=self.user_id,
        )
        if not isinstance(decision, ManagementDecisionV2):
            try:
                decision = ManagementDecisionV2.model_validate(decision)
            except Exception as exc:
                raise ValueError(
                    f"PM returned an invalid management decision: {exc}"
                ) from exc
        if getattr(decision, "action", None) not in PM_V1_ACTIONS:
            decision = _fail_closed_decision(decision, ManagementDecisionV2)
        from condor.brooks.market_analysis import check_pm_anti_loop_guard

        check_pm_anti_loop_guard(
            getattr(decision, "action", None),
            prior_market_analysis=context.get("market_analysis"),
        )
        if getattr(decision, "decision_time_ms", None) != int(
            context["decision_time_ms"]
        ):
            raise ValueError("PM decision time differs from its read snapshot")
        owned_ids = {
            str(_as_dict(position).get("position_id"))
            for position in (context.get("positions") or [context.get("position")])
            if position is not None and _as_dict(position).get("position_id")
        }
        requested_ids = set(getattr(decision, "position_ids", []))
        if requested_ids and not requested_ids.issubset(owned_ids):
            raise ValueError("PM decision references an unbound position")
        await _resolve(self.save_decision(correlation_id, decision))
        callback = getattr(self.publish, "publish", self.publish)
        orig_intent = context.get("original_trade_intent")
        is_shadow = bool(
            context.get("shadow_mode")
            or envelope.get("shadow_mode")
            or (isinstance(envelope.get("payload"), Mapping) and envelope["payload"].get("shadow_mode"))
            or (isinstance(orig_intent, Mapping) and orig_intent.get("shadow_mode"))
            or (hasattr(orig_intent, "shadow_mode") and getattr(orig_intent, "shadow_mode"))
            or getattr(decision, "shadow_mode", False)
        )
        if is_shadow and hasattr(decision, "model_copy"):
            decision = decision.model_copy(update={"shadow_mode": True})
        payload = (
            decision.model_dump(mode="json")
            if hasattr(decision, "model_dump")
            else dict(decision)
        )
        payload["shadow_mode"] = is_shadow
        event_data = {
            "schema": "condor.brooks.event.v1",
            "event_id": str(uuid4()),
            "created_at_ms": time.time_ns() // 1_000_000,
            "type": "MANAGEMENT_INTENT_CREATED",
            "symbol": context["symbol"],
            "correlation_id": correlation_id,
            "causation_id": envelope.get("event_id"),
            "shadow_mode": is_shadow,
            "payload": payload,
        }
        if hasattr(self.publish, "publish"):
            from condor.brooks.events import BrooksEvent, EventType

            event = BrooksEvent(
                type=EventType.MANAGEMENT_INTENT_CREATED,
                symbol=event_data["symbol"],
                payload=event_data["payload"],
                correlation_id=correlation_id,
                causation_id=event_data["causation_id"],
                event_id=event_data["event_id"],
                created_at_ms=event_data["created_at_ms"],
            )
        else:
            event = event_data
        await _resolve(callback(event))
        return decision

    async def consume(self, queue: asyncio.Queue[Any]) -> None:
        """Run independently on a subscribed bus queue; supervisor cancels task."""
        while True:
            await self.handle_event(await queue.get())

    @staticmethod
    def _has_active_position(context: Mapping[str, Any]) -> bool:
        if "position_active" in context:
            return bool(context["position_active"])
        positions = context.get("positions")
        if positions is None:
            positions = [context.get("position")]
        for value in positions:
            if value is None:
                continue
            data = _as_dict(value)
            try:
                if (
                    float(data.get("quantity", data.get("qty", data.get("size", 0))))
                    > 0
                ):
                    return True
            except (TypeError, ValueError):
                pass
        return False


def _fail_closed_decision(decision: Any, output_model: Any) -> Any:
    try:
        payload = decision.model_dump(mode="json")
    except Exception as exc:
        raise ValueError(f"PM returned an undecodable decision: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("PM decision dump must be a mapping")
    payload["action"] = "MANAGEMENT_BLOCKED"
    return output_model.model_validate(payload)
