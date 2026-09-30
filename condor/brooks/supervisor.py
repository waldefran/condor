"""Independent Brooks lifecycle over the bus and store.

NEVER a ``PM -> Trader -> GM`` single-tick pipeline. :meth:`BrooksSupervisor.start`
registers independent asyncio tasks via :meth:`add_child`, each with its
own cadence or event subscription:

- MarketClock (``condor/brooks/clock.py``): H1/H4/D1 closed-bar publishes.
- TraderConsumer (``trader.py``): H1_BAR_CLOSED -> TradeIntentV2 ->
  TRADER_INTENT_CREATED.
- ContextAnalystConsumer (``htf_analyst.py``): D1/H4 closed bars -> typed
  MarketContextV2 documents -> MARKET_CONTEXT_UPDATED.
- PMTimer: periodic PM_TIMER from ``BrooksConfig.pm``.
- PositionWatcher (``position_watcher.py``): transition events at the
  ``position_watcher`` cadence.
- PMConsumer (``pm.PositionManager``): wake-gated management decisions ->
  MANAGEMENT_INTENT_CREATED.
- GMConsumer (below): entry/management routing through the deterministic GM;
  nothing outside GM ever writes.

Integration seams (every external collaborator is injected via constructor
kwargs or ``attach_*`` methods with test fakes; real Condor/Hummingbot
adapters stay behind these narrow callables and are owned by other worktrees):

- ``symbols``: venue symbols the clock publishes for (``attach_symbols``).
- ``candle_source``: read-only ``CandleSource`` (or async callable
  ``(symbol, timeframe, limit)``) shared by the clock and the role consumers.
- ``agent_key`` (+ optional ``user_id``): builds the real Trader/HTF
  consumers when no ``trader_handler`` / ``htf_handler`` fake is injected.
- ``trader_handler`` / ``htf_handler``: ``async (event) -> result`` fakes or
  custom handlers used instead of the real role consumers.
- ``watcher_snapshots``: ``() -> iterable`` of Brooks-bound venue snapshots
  for ``PositionWatcher`` (may be async). Defaults to ``[]`` (idle polls).
- ``pm_runner``, ``pm_load_context``, ``pm_save_decision``,
  ``pm_record_market_read``, ``pm_list_active``: the ``PositionManager``
  seams from ``pm.py``. Defaults are store-backed and idle without venue
  bindings (``load_context`` returns ``None``); ``pm_handler`` replaces the
  whole PM step with ``async (event) -> decision``.
- ``gm_factory``: ``(symbol) -> BrooksGM`` (any object with async
  ``execute_entry`` / ``execute_management``). Without it the GM consumer
  fails closed with RECONCILIATION_REQUIRED.
- ``now_fn`` / ``sleep_fn``: injected clock and sleeper for the clock,
  PM timer, and watcher loops (prefer fakes over real sleeps in tests).

Public surface used by ``condor/agents/engine.py``
(``start``/``run``/``pause``/``resume``/``wait_until_resumed``/``add_child``/``stop``)
is unchanged and additive only; ``execution_mode=loop`` never touches this
module.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .clock import MarketClock
from .config import BrooksConfig
from .events import BrooksEvent, EventBus, EventType
from .pm import PM_WAKE_EVENTS, PositionManager
from .position_watcher import PositionWatcher
from .store import BrooksStore

log = logging.getLogger(__name__)

_CHILD_NAMES = (
    "brooks-market-clock",
    "brooks-trader",
    "brooks-d1-context",
    "brooks-h4-context",
    "brooks-context-bootstrap",
    "brooks-pm-timer",
    "brooks-watcher",
    "brooks-pm",
    "brooks-gm",
)

_NO_WRITE_ACTIONS = frozenset({"MANAGEMENT_BLOCKED"})
_ATTENTION_ACTIONS = frozenset({"RECONCILE_STATE"})


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError("GM consumer events must be mappings or Pydantic models")


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    if is_dataclass(value) and not isinstance(value, type):
        dumped = asdict(value)
        if isinstance(dumped, dict):
            return dumped
    raise TypeError("GM consumer payloads must be mappings or Pydantic models")


class GMConsumer:
    """Route intents through the deterministic GM; never write directly.

    - ``TRADER_INTENT_CREATED`` with ENTER_* -> ``gm.execute_entry`` ->
      ``GM_ENTRY_APPROVED``; ``NO_TRADE`` is an intentional no-op.
    - ``MANAGEMENT_INTENT_CREATED`` with HOLD/REDUCE/CLOSE/hedge actions ->
      ``gm.execute_management`` (the existing GM management API, which rejects
      what it cannot safely compile) -> ``GM_MANAGEMENT_APPROVED``.
    - ``MANAGEMENT_BLOCKED`` is an explicit no-write: nothing is published.
    - ``REQUEST_MARKET_ANALYSIS`` / ``RECONCILE_STATE`` need fresh analysis or
      state, not a write: ``RECONCILIATION_REQUIRED``.
    - ``GMRejected`` -> ``GM_ENTRY_REJECTED`` / ``GM_MANAGEMENT_REJECTED``;
      any other (ambiguous) failure -> ``RECONCILIATION_REQUIRED`` so a
      timeout that may have been accepted is reconciled, never retried blind.
    """

    def __init__(
        self,
        *,
        gm_factory: Callable[[str], Any] | None,
        publish: Any,
        market_analysis_runner: Callable[[Any], Awaitable[Any] | Any] | None = None,
        agent_key: str | None = None,
        candle_source: Any | None = None,
        tools: Sequence[Any] | None = None,
        store: Any | None = None,
        user_id: int | None = None,
        now_fn: Callable[[], int] | None = None,
        raise_boundary_errors: bool = False,
    ) -> None:
        self.gm_factory = gm_factory
        self.publish = publish
        self.market_analysis_runner = market_analysis_runner
        self.agent_key = agent_key
        self.candle_source = candle_source
        self.tools = tools
        self.store = store
        self.user_id = user_id
        self.now_fn = now_fn
        self.raise_boundary_errors = raise_boundary_errors
        self._latest_analysis: dict[str, Any] = {}

    async def reconcile_bound_snapshot(
        self, snapshot: Mapping[str, Any], *, shadow_mode: bool = False
    ) -> None:
        """Use watcher hints to request fresh GM proof, never as write authority."""
        if (
            self.gm_factory is None
            or Decimal(str(snapshot.get("main", {}).get("qty", 0))) > 0
        ):
            return
        symbol, correlation = snapshot.get("symbol"), snapshot.get("correlation_id")
        if not symbol or not correlation:
            return
        gm = self.gm_factory(symbol)
        if inspect.isawaitable(gm):
            gm = await gm
        reconcile = getattr(gm, "reconcile_lifecycle", None)
        if reconcile is None:
            return
        try:
            path = gm._trade_dir(correlation) / "binding.json"
            before = json.loads(path.read_text(encoding="utf-8")).get("status")
            result = await reconcile(correlation, shadow_mode=shadow_mode)
            if result.get("status") == "closed" and before != "closed":
                await self._emit(
                    EventType.BINDING_CLOSED,
                    symbol,
                    correlation,
                    None,
                    {"binding": result},
                )
        except Exception:
            # Venue failures cannot fabricate closure or kill the supervisor.
            log.warning(
                "Brooks lifecycle reconciliation deferred for %s",
                correlation,
                exc_info=True,
            )

    def _build_default_analyst_runner(
        self, symbol: str
    ) -> Callable[[Any], Awaitable[Any]]:
        async def _run(clean_req: Any) -> Any:
            from .agent_runner import run_role
            from .agent_runner import bind_symbol_tools
            from .contracts import TradeIntentV2
            from .market_analysis import filter_read_only_tools
            from .market_tools import TraderMarketTools

            decision_time = getattr(clean_req, "decision_time_ms", None)
            if decision_time is None and isinstance(clean_req, Mapping):
                decision_time = clean_req.get("decision_time_ms")
            market_tools = TraderMarketTools(
                source=self.candle_source,
                decision_time_ms=decision_time,
            )
            role_tools = {
                "get_closed_candles": market_tools.get_closed_candles,
                "get_recent_structure": market_tools.get_recent_structure,
                "get_volatility": market_tools.get_volatility,
            }
            safe_tools = filter_read_only_tools(list(role_tools.values()))
            named = {tool.__name__: tool for tool in safe_tools}
            prompt = (
                clean_req.model_dump()
                if hasattr(clean_req, "model_dump")
                else dict(clean_req)
            )
            return await run_role(
                "TRADER",
                agent_key=self.agent_key,
                prompt=prompt,
                output_model=TradeIntentV2,
                market_tools=bind_symbol_tools(symbol, named),
                user_id=self.user_id,
            )

        return _run

    async def _emit(
        self,
        event_type: EventType,
        symbol: str,
        correlation_id: str | None,
        causation_id: str | None,
        payload: dict[str, Any],
    ) -> BrooksEvent | dict[str, Any]:
        envelope = {
            "schema": "condor.brooks.event.v1",
            "event_id": str(uuid4()),
            "type": event_type.value,
            "created_at_ms": time.time_ns() // 1_000_000,
            "symbol": symbol,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "payload": payload,
        }
        callback = getattr(self.publish, "publish", self.publish)
        if hasattr(self.publish, "publish"):
            event = BrooksEvent(
                type=event_type,
                symbol=symbol,
                payload=payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
                event_id=envelope["event_id"],
                created_at_ms=envelope["created_at_ms"],
            )
        else:
            event = envelope
        result = callback(event)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            await result
        return event

    def _gm_for(self, symbol: str) -> Any:
        if self.gm_factory is None:
            raise RuntimeError("Brooks GM is not configured")
        return self.gm_factory(symbol)

    async def handle(self, event: BrooksEvent | Mapping[str, Any]) -> Any | None:
        from .gm import GMRejected

        envelope = _as_dict(event)
        raw_type = envelope.get("type")
        event_type = (
            raw_type.value if isinstance(raw_type, EventType) else str(raw_type or "")
        )
        symbol = str(envelope.get("symbol") or "")
        correlation_id = envelope.get("correlation_id")
        causation_id = envelope.get("event_id")
        payload = _as_dict(envelope.get("payload") or {})
        if not symbol:
            raise ValueError("GM consumer requires an event symbol")

        if event_type == EventType.TRADER_INTENT_CREATED.value:
            intent = _as_dict(payload.get("intent") or {})
            decision = str(intent.get("decision") or "")
            if decision == "NO_TRADE":
                return None
            if bool(
                payload.get("shadow_mode", False) or intent.get("shadow_mode", False)
            ):
                return None
            if decision not in ("ENTER_LONG", "ENTER_SHORT"):
                return await self._emit(
                    EventType.RECONCILIATION_REQUIRED,
                    symbol,
                    correlation_id if isinstance(correlation_id, str) else None,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": f"ambiguous trader decision: {decision!r}"},
                )
            if not isinstance(correlation_id, str) or not correlation_id:
                return await self._emit(
                    EventType.RECONCILIATION_REQUIRED,
                    symbol,
                    None,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": "entry intent lacks correlation_id"},
                )
            try:
                binding = await self._gm_for(symbol).execute_entry(
                    intent, correlation_id=correlation_id
                )
            except GMRejected as exc:
                return await self._emit(
                    EventType.GM_ENTRY_REJECTED,
                    symbol,
                    correlation_id,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": str(exc)},
                )
            except Exception as exc:
                return await self._emit(
                    EventType.RECONCILIATION_REQUIRED,
                    symbol,
                    correlation_id,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": f"ambiguous entry outcome: {exc!r}"},
                )
            if binding is None:
                return None
            return await self._emit(
                EventType.GM_ENTRY_APPROVED,
                symbol,
                correlation_id,
                causation_id if isinstance(causation_id, str) else None,
                {"binding": _dump(binding)},
            )

        if event_type == EventType.MANAGEMENT_INTENT_CREATED.value:
            is_shadow = bool(
                payload.get("shadow_mode", False)
                or envelope.get("shadow_mode", False)
            )
            if not is_shadow and isinstance(correlation_id, str) and correlation_id:
                try:
                    trade_dir = None
                    if self.store is not None and hasattr(self.store, "_trade_dir"):
                        trade_dir = self.store._trade_dir(correlation_id)
                    elif self.gm_factory is not None:
                        gm = self._gm_for(symbol)
                        if hasattr(gm, "_trade_dir"):
                            trade_dir = gm._trade_dir(correlation_id)
                    if trade_dir is not None:
                        for name in ("binding.json", "original_trade_intent.json"):
                            path = trade_dir / name
                            if path.exists():
                                doc = json.loads(path.read_text(encoding="utf-8"))
                                if isinstance(doc, dict) and bool(
                                    doc.get("shadow_mode", False)
                                    or (isinstance(doc.get("intent"), dict) and doc["intent"].get("shadow_mode", False))
                                ):
                                    is_shadow = True
                                    break
                except Exception:
                    pass
            if is_shadow:
                return None
            action = str(payload.get("action") or "")
            if action in _NO_WRITE_ACTIONS:
                return None

            if action == "REQUEST_MARKET_ANALYSIS":
                raw_req = payload.get("market_analysis_request")
                if raw_req is None:
                    return await self._emit(
                        EventType.RECONCILIATION_REQUIRED,
                        symbol,
                        correlation_id if isinstance(correlation_id, str) else None,
                        causation_id if isinstance(causation_id, str) else None,
                        {
                            "reason": "REQUEST_MARKET_ANALYSIS lacks market_analysis_request"
                        },
                    )

                if correlation_id and correlation_id in self._latest_analysis:
                    from .market_analysis import check_pm_anti_loop_guard

                    try:
                        check_pm_anti_loop_guard(
                            "REQUEST_MARKET_ANALYSIS",
                            prior_market_analysis=self._latest_analysis[correlation_id],
                        )
                    except Exception as exc:
                        if self.raise_boundary_errors:
                            raise
                        return await self._emit(
                            EventType.RECONCILIATION_REQUIRED,
                            symbol,
                            correlation_id,
                            causation_id if isinstance(causation_id, str) else None,
                            {
                                "reason": f"anti-loop guard blocked back-to-back analysis: {exc}"
                            },
                        )

                runner = self.market_analysis_runner
                if (
                    runner is None
                    and self.agent_key is not None
                    and self.candle_source is not None
                ):
                    runner = self._build_default_analyst_runner(symbol)

                if runner is None:
                    return await self._emit(
                        EventType.RECONCILIATION_REQUIRED,
                        symbol,
                        correlation_id if isinstance(correlation_id, str) else None,
                        causation_id if isinstance(causation_id, str) else None,
                        {"reason": "market analysis runner is not configured"},
                    )

                from .market_analysis import (
                    MarketAnalysisBoundaryError,
                    async_execute_fresh_market_analysis_handshake,
                )

                try:
                    analysis_response = (
                        await async_execute_fresh_market_analysis_handshake(
                            request=raw_req,
                            trader_runner=runner,
                            tools=self.tools,
                        )
                    )
                except MarketAnalysisBoundaryError as exc:
                    if self.raise_boundary_errors:
                        raise
                    return await self._emit(
                        EventType.RECONCILIATION_REQUIRED,
                        symbol,
                        correlation_id if isinstance(correlation_id, str) else None,
                        causation_id if isinstance(causation_id, str) else None,
                        {"reason": f"market analysis boundary violation: {exc}"},
                    )
                except Exception as exc:
                    if self.raise_boundary_errors:
                        raise
                    return await self._emit(
                        EventType.RECONCILIATION_REQUIRED,
                        symbol,
                        correlation_id if isinstance(correlation_id, str) else None,
                        causation_id if isinstance(causation_id, str) else None,
                        {"reason": f"market analysis execution failed: {exc}"},
                    )

                if correlation_id:
                    self._latest_analysis[correlation_id] = analysis_response

                resp_dump = analysis_response.model_dump(mode="json")
                if self.store is not None and hasattr(
                    self.store, "save_market_analysis"
                ):
                    self.store.save_market_analysis(correlation_id, resp_dump)

                return await self._emit(
                    EventType.MARKET_ANALYSIS_COMPLETED,
                    symbol,
                    correlation_id if isinstance(correlation_id, str) else None,
                    causation_id if isinstance(causation_id, str) else None,
                    {"market_analysis": resp_dump},
                )

            if correlation_id and correlation_id in self._latest_analysis:
                self._latest_analysis.pop(correlation_id, None)

            if action in _ATTENTION_ACTIONS or not action:
                return await self._emit(
                    EventType.RECONCILIATION_REQUIRED,
                    symbol,
                    correlation_id if isinstance(correlation_id, str) else None,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": f"management action needs attention: {action!r}"},
                )
            if not isinstance(correlation_id, str) or not correlation_id:
                return await self._emit(
                    EventType.RECONCILIATION_REQUIRED,
                    symbol,
                    None,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": "management intent lacks correlation_id"},
                )
            try:
                gm = self._gm_for(symbol)
                sig = inspect.signature(gm.execute_management)
                if "decision" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                ):
                    result = await gm.execute_management(
                        correlation_id=correlation_id,
                        decision=payload,
                    )
                else:
                    decision_id = payload.get("decision_id")
                    if not isinstance(decision_id, str) or not decision_id:
                        decision_id = (
                            causation_id
                            if isinstance(causation_id, str) and causation_id
                            else str(uuid4())
                        )
                    result = await gm.execute_management(
                        correlation_id=correlation_id,
                        decision_id=decision_id,
                        action=action,
                        reduce_fraction=payload.get("reduce_fraction"),
                    )
            except GMRejected as exc:
                return await self._emit(
                    EventType.GM_MANAGEMENT_REJECTED,
                    symbol,
                    correlation_id,
                    causation_id if isinstance(causation_id, str) else None,
                    {"reason": str(exc), "action": action},
                )
            except Exception as exc:
                return await self._emit(
                    EventType.RECONCILIATION_REQUIRED,
                    symbol,
                    correlation_id,
                    causation_id if isinstance(causation_id, str) else None,
                    {
                        "reason": f"ambiguous management outcome: {exc!r}",
                        "action": action,
                    },
                )
            return await self._emit(
                EventType.GM_MANAGEMENT_APPROVED,
                symbol,
                correlation_id,
                causation_id if isinstance(causation_id, str) else None,
                {"result": _dump(result), "action": action},
            )

        raise ValueError(f"GM consumer does not handle {event_type!r}")


class BrooksSupervisor:
    def __init__(
        self,
        strategy_home: Path,
        config: BrooksConfig,
        *,
        symbols: Iterable[str] = (),
        candle_source: Any | None = None,
        agent_key: str | None = None,
        user_id: int | None = None,
        trader_handler: Callable[[BrooksEvent], Awaitable[Any]] | None = None,
        htf_handler: Callable[[BrooksEvent], Awaitable[Any]] | None = None,
        pm_handler: Callable[[Any], Awaitable[Any]] | None = None,
        pm_runner: Any | None = None,
        pm_load_context: Callable[[str], Any] | None = None,
        pm_save_decision: Callable[[str, Any], Any] | None = None,
        pm_record_market_read: Callable[[str, dict[str, Any]], Any] | None = None,
        pm_list_active: Callable[[Any], Any] | None = None,
        gm_factory: Callable[[str], Any] | None = None,
        watcher_snapshots: Callable[[], Any] | None = None,
        market_analysis_runner: Any | None = None,
        tools: Sequence[Any] | None = None,
        now_fn: Callable[[], int] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.strategy_home = Path(strategy_home)
        self.config = config
        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._children: set[asyncio.Task] = set()
        self._started = False
        self.store: BrooksStore | None = None
        self.events: EventBus | None = None
        self._symbols = list(symbols)
        self._candle_source = candle_source
        self._agent_key = agent_key
        self._user_id = user_id
        self._shadow_mode = config.shadow_mode
        self._trader_handler = trader_handler
        self._htf_handler = htf_handler
        self._pm_handler = pm_handler
        self._pm_runner = pm_runner
        self._pm_load_context = pm_load_context
        self._pm_save_decision = pm_save_decision
        self._pm_record_market_read = pm_record_market_read
        self._pm_list_active = pm_list_active
        self._gm_factory = gm_factory
        self._watcher_snapshots = watcher_snapshots
        self._market_analysis_runner = market_analysis_runner
        self._tools = list(tools) if tools is not None else None
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._clock: MarketClock | None = None
        self._trader: Any | None = None
        self._htf: Any | None = None
        self._h4: Any | None = None
        self._watcher: PositionWatcher | None = None
        self._pm: PositionManager | None = None
        self._gm: GMConsumer | None = None

    # -- injection (attach before start; handler/factory swaps also apply live)
    def attach_tools(self, tools: Sequence[Any] | None) -> None:
        self._tools = list(tools) if tools is not None else None
        if self._gm is not None:
            self._gm.tools = self._tools

    def attach_market_analysis_runner(self, runner: Any) -> None:
        self._market_analysis_runner = runner
        if self._gm is not None:
            self._gm.market_analysis_runner = runner

    def attach_symbols(self, symbols: Iterable[str]) -> None:
        self._symbols = list(symbols)

    def attach_candle_source(self, source: Any) -> None:
        self._candle_source = source

    def attach_agent_key(self, agent_key: str, user_id: int | None = None) -> None:
        self._agent_key = agent_key
        self._user_id = user_id

    def attach_user_id(self, user_id: int | None) -> None:
        self._user_id = user_id

    def attach_shadow_mode(self, shadow_mode: bool) -> None:
        """Live vs decision-only for the Trader consumer; attach before start."""
        self._shadow_mode = bool(shadow_mode)

    def attach_trader_handler(
        self, handler: Callable[[BrooksEvent], Awaitable[Any]]
    ) -> None:
        self._trader_handler = handler

    def attach_htf_handler(
        self, handler: Callable[[BrooksEvent], Awaitable[Any]]
    ) -> None:
        self._htf_handler = handler

    def attach_pm_handler(self, handler: Callable[[Any], Awaitable[Any]]) -> None:
        self._pm_handler = handler

    def attach_pm(
        self,
        *,
        runner: Any = None,
        load_context: Callable[[str], Any] | None = None,
        save_decision: Callable[[str, Any], Any] | None = None,
        record_market_read: Callable[[str, dict[str, Any]], Any] | None = None,
        list_active: Callable[[Any], Any] | None = None,
    ) -> None:
        if runner is not None:
            self._pm_runner = runner
        if load_context is not None:
            self._pm_load_context = load_context
        if save_decision is not None:
            self._pm_save_decision = save_decision
        if record_market_read is not None:
            self._pm_record_market_read = record_market_read
        if list_active is not None:
            self._pm_list_active = list_active

    def attach_gm_factory(self, factory: Callable[[str], Any]) -> None:
        self._gm_factory = factory

    def attach_watcher_snapshots(self, provider: Callable[[], Any]) -> None:
        self._watcher_snapshots = provider

    def attach_clocks(
        self,
        now_fn: Callable[[], int] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if now_fn is not None:
            self._now_fn = now_fn
            if self._clock is not None:
                self._clock.now_fn = now_fn
        if sleep_fn is not None:
            self._sleep_fn = sleep_fn
            if self._clock is not None:
                self._clock.sleep_fn = sleep_fn

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop.is_set()

    async def start(self) -> None:
        if self._started:
            return
        self.store = BrooksStore(self.strategy_home)
        self.events = EventBus(self.store)
        self._stop.clear()
        self._resume.set()
        self._build_children()
        self._started = True
        for name, coro in (
            ("brooks-trader", self._trader_loop()),
            ("brooks-d1-context", self._htf_loop()),
            ("brooks-h4-context", self._h4_loop()),
            ("brooks-pm-timer", self._pm_timer_loop()),
            ("brooks-watcher", self._watcher_loop()),
            ("brooks-pm", self._pm_loop()),
            ("brooks-gm", self._gm_loop()),
            ("brooks-market-clock", self._clock_loop()),
            ("brooks-context-bootstrap", self._bootstrap_contexts()),
        ):
            self.add_child(asyncio.create_task(coro, name=name))

    def _build_children(self) -> None:
        assert self.store is not None and self.events is not None
        clock_kwargs: dict[str, Any] = {
            "symbols": list(self._symbols),
            "trader": self.config.trader,
            "htf": self.config.htf,
            "h4": self.config.h4,
            "source": self._candle_source,
            "publish": self.events,
        }
        if self._now_fn is not None:
            clock_kwargs["now_fn"] = self._now_fn
        if self._sleep_fn is not None:
            clock_kwargs["sleep_fn"] = self._sleep_fn
        self._clock = MarketClock(**clock_kwargs)
        self._trader = self._build_trader()
        self._htf = self._build_htf()
        self._h4 = self._build_h4()
        self._watcher = PositionWatcher(
            self._watcher_snapshots or (lambda: []),
            self.events,
            on_snapshot=lambda snapshot: self._gm.reconcile_bound_snapshot(
                snapshot, shadow_mode=self._shadow_mode
            ),
        )
        self._pm = self._build_pm()
        self._gm = GMConsumer(
            gm_factory=self._gm_factory,
            publish=self.events,
            market_analysis_runner=self._market_analysis_runner,
            agent_key=self._agent_key,
            candle_source=self._candle_source,
            tools=self._tools,
            store=self.store,
            user_id=self._user_id,
            now_fn=self._now_fn,
        )

    def _build_trader(self) -> Any | None:
        key = self.config.trader_agent_key or self._agent_key
        if (
            self._trader_handler is not None
            or key is None
            or self._candle_source is None
        ):
            return None
        from .trader import TraderConsumer

        assert self.store is not None and self.events is not None
        return TraderConsumer(
            agent_key=key,
            source=self._candle_source,
            store=self.store,
            events=self.events,
            user_id=self._user_id,
            shadow_mode=self._shadow_mode,
            timeout_sec=self.config.trader_timeout_sec,
            max_role_attempts=self.config.max_role_attempts,
            retry_backoff_sec=self.config.retry_backoff_sec,
        )

    def _build_htf(self) -> Any | None:
        key = self.config.d1_agent_key or self._agent_key
        if (
            self._htf_handler is not None
            or key is None
            or self._candle_source is None
        ):
            return None
        from .htf_analyst import ContextAnalystConsumer

        assert self.store is not None and self.events is not None
        return ContextAnalystConsumer(
            agent_key=key,
            source=self._candle_source,
            store=self.store,
            events=self.events,
            timeframe="1d",
            user_id=self._user_id,
            timeout_sec=self.config.context_timeout_sec,
            max_role_attempts=self.config.max_role_attempts,
            retry_backoff_sec=self.config.retry_backoff_sec,
        )

    def _build_h4(self) -> Any | None:
        key = self.config.h4_agent_key or self._agent_key
        if key is None or self._candle_source is None:
            return None
        from .htf_analyst import ContextAnalystConsumer

        assert self.store is not None and self.events is not None
        return ContextAnalystConsumer(
            agent_key=key,
            source=self._candle_source,
            store=self.store,
            events=self.events,
            timeframe="4h",
            user_id=self._user_id,
            timeout_sec=self.config.context_timeout_sec,
            max_role_attempts=self.config.max_role_attempts,
            retry_backoff_sec=self.config.retry_backoff_sec,
        )

    def _build_pm(self) -> PositionManager | None:
        if self._pm_handler is not None:
            return None
        if self._pm_runner is None and self._agent_key is None:
            log.info("Brooks PM idle: no runner or agent key configured")
            return None
        return PositionManager(
            runner=self._pm_runner,
            load_context=self._pm_load_context or self._default_pm_load,
            save_decision=self._pm_save_decision or self._default_pm_save,
            publish=self.events,
            candle_source=self._candle_source,
            record_market_read=self._pm_record_market_read or self._default_pm_record,
            list_active_correlations=self._pm_list_active
            or self._default_pm_list_active,
            agent_key=self._agent_key,
        )

    async def _default_pm_load(self, correlation_id: str) -> dict[str, Any] | None:
        # No venue integration in this worktree: wakes are no-ops until the
        # Integrator replaces this with a venue-backed snapshot builder.
        return None

    async def _default_pm_save(self, correlation_id: str, decision: Any) -> None:
        assert self.store is not None
        dumped = (
            decision.model_dump(mode="json")
            if hasattr(decision, "model_dump")
            else dict(decision)
        )
        self.store.write_trade_document(
            correlation_id, "latest_management_intent.json", dumped
        )
        self.store.append_trade_history(
            correlation_id,
            "management_history.jsonl",
            {
                "action": dumped.get("action"),
                "decision_time_ms": dumped.get("decision_time_ms"),
            },
        )

    async def _default_pm_record(
        self, correlation_id: str, record: dict[str, Any]
    ) -> None:
        return None

    async def _default_pm_list_active(self, symbol: Any) -> list[str]:
        assert self.store is not None
        # ONE root: check both strategy_home / "trades" and self.store.root / "trades"
        # without duplicates.
        active: list[str] = []
        seen: set[str] = set()
        for root_dir in (self.strategy_home / "trades", self.store.root / "trades"):
            if not root_dir.exists():
                continue
            for binding_path in sorted(root_dir.glob("*/binding.json")):
                cid = binding_path.parent.name
                if cid in seen:
                    continue
                try:
                    import json

                    binding = json.loads(binding_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(binding, dict):
                    continue
                if symbol not in (None, "", "*") and binding.get("symbol") != symbol:
                    continue
                seen.add(cid)
                active.append(cid)
        return active

    async def run(self) -> None:
        await self._stop.wait()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    async def wait_until_resumed(self) -> None:
        await self._resume.wait()

    def add_child(self, task: asyncio.Task) -> None:
        """Give the supervisor ownership of a future worker task."""
        if not self.is_running:
            raise RuntimeError("Brooks supervisor is not running")
        self._children.add(task)
        task.add_done_callback(self._children.discard)

    async def _sleep(self, delay_sec: float) -> None:
        if delay_sec <= 0:
            await asyncio.sleep(0)
            return
        if self._sleep_fn is not None:
            await self._sleep_fn(delay_sec)
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay_sec)
        except TimeoutError:
            pass

    async def _consume_loop(
        self,
        types: set[EventType],
        handle: Callable[[BrooksEvent], Awaitable[Any]],
        owner: str,
    ) -> None:
        assert self.events is not None
        queue = self.events.subscribe(types)
        try:
            while not self._stop.is_set():
                event = await queue.get()
                await self.wait_until_resumed()
                if self._stop.is_set():
                    break
                try:
                    await handle(event)
                except Exception:
                    log.exception(
                        "Brooks %s failed for event %s", owner, event.event_id
                    )
        finally:
            self.events.unsubscribe(queue)

    async def _clock_loop(self) -> None:
        assert self._clock is not None
        await self._clock.run(self._stop, self.wait_until_resumed)

    async def _trader_loop(self) -> None:
        async def handle(event: BrooksEvent) -> Any | None:
            if self._trader_handler is not None:
                return await self._trader_handler(event)
            if self._trader is not None:
                return await self._trader.handle(event)
            return None

        await self._consume_loop({EventType.H1_BAR_CLOSED}, handle, "Trader")

    async def _htf_loop(self) -> None:
        async def handle(event: BrooksEvent) -> Any | None:
            if self._htf_handler is not None:
                return await self._htf_handler(event)
            if self._htf is not None:
                return await self._htf.handle(event)
            return None

        await self._consume_loop({EventType.D1_BAR_CLOSED}, handle, "HTF Analyst")

    async def _h4_loop(self) -> None:
        if self._h4 is None:
            await self._stop.wait()
            return
        await self._consume_loop(
            {EventType.H4_BAR_CLOSED}, self._h4.handle, "H4 Context Analyst"
        )

    async def _bootstrap_contexts(self) -> None:
        """Fill missing/stale macro contexts from the latest closed bars."""
        if self._candle_source is None or self.store is None:
            return
        from .clock import latest_due_close, timeframe_ms

        now_ms = self._now_fn() if self._now_fn else time.time_ns() // 1_000_000

        async def fill(
            timeframe: str, consumer: Any, event_type: EventType, symbol: str
        ) -> None:
            closed_at = latest_due_close(now_ms, timeframe_ms(timeframe), 0)
            if self._stop.is_set():
                return
            try:
                label = "D1" if timeframe == "1d" else "H4"
                latest = self.store.read_market_context(label, symbol=symbol)
                if latest and latest.get("decision_time_ms") == closed_at and latest.get("window_bars") == 120:
                    return
                await consumer.handle(
                    BrooksEvent(
                        type=event_type,
                        symbol=symbol,
                        correlation_id=f"{symbol}-{timeframe}-{closed_at}-bootstrap",
                        payload={
                            "decision_time_ms": closed_at,
                            "close_time_ms": closed_at,
                            "timeframe": timeframe,
                            "bootstrap": True,
                        },
                    )
                )
            except Exception:
                log.exception("Brooks %s context bootstrap failed for %s", timeframe, symbol)

        tasks = [
            fill(timeframe, consumer, event_type, symbol)
            for timeframe, consumer, event_type in (
                ("1d", self._htf, EventType.D1_BAR_CLOSED),
                ("4h", self._h4, EventType.H4_BAR_CLOSED),
            )
            if consumer is not None
            for symbol in self._symbols
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def _pm_timer_loop(self) -> None:
        assert self.events is not None
        frequency_sec = self.config.pm.frequency_sec
        while not self._stop.is_set():
            await self.wait_until_resumed()
            if self._stop.is_set():
                break
            await self._sleep(frequency_sec)
            if self._stop.is_set():
                break
            await self.wait_until_resumed()
            if self._stop.is_set():
                break
            try:
                await self.events.publish(
                    BrooksEvent(
                        type=EventType.PM_TIMER,
                        symbol="*",
                        payload={"frequency_sec": frequency_sec},
                    )
                )
            except Exception:
                log.exception("Brooks PM timer publish failed")

    async def _watcher_loop(self) -> None:
        assert self._watcher is not None
        frequency_sec = self.config.position_watcher.frequency_sec
        while not self._stop.is_set():
            await self.wait_until_resumed()
            if self._stop.is_set():
                break
            try:
                await self._watcher.poll()
            except Exception:
                log.exception("Brooks position watcher poll failed")
            await self._sleep(frequency_sec)

    async def _pm_loop(self) -> None:
        async def handle(event: BrooksEvent) -> Any | None:
            if self._pm_handler is not None:
                return await self._pm_handler(event)
            if self._pm is not None:
                return await self._pm.handle_event(event)
            return None

        await self._consume_loop(
            {EventType(name) for name in PM_WAKE_EVENTS}, handle, "PositionManager"
        )

    async def _gm_loop(self) -> None:
        async def handle(event: BrooksEvent) -> Any | None:
            assert self._gm is not None
            try:
                return await self._gm.handle(event)
            except ValueError:
                log.exception("Brooks GM consumer received an invalid event")

        await self._consume_loop(
            {EventType.TRADER_INTENT_CREATED, EventType.MANAGEMENT_INTENT_CREATED},
            handle,
            "GM",
        )

    async def stop(self) -> None:
        self._stop.set()
        children = tuple(self._children)
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        self._children.clear()
        if self.events is not None:
            self.events.close()
        if self.store is not None:
            self.store.flush()
        self._started = False
