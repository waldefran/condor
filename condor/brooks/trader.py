"""H1 Brooks Trader: frozen market snapshot, durable intent, then event."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from condor.brooks.agent_runner import bind_symbol_tools, run_role
from condor.brooks.contracts import MarketContextV2, TradeIntentV2, _decimal
from condor.brooks.decision_cycles import (
    DecisionCycleStore,
    MemoryDecisionCycleStore,
)
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.llm_coordination import (
    backend_call_slot,
    backend_resource_key,
    is_transient_role_error,
)
from condor.brooks.market_tools import CandleSource, TraderMarketTools
from condor.brooks.store import BrooksStore

log = logging.getLogger(__name__)

_INTERVAL_MS = {"M15": 900_000, "H1": 3_600_000, "H4": 14_400_000, "D1": 86_400_000}
_TIMEFRAME_ALIASES = {
    "15M": "M15",
    "M15": "M15",
    "1H": "H1",
    "H1": "H1",
    "4H": "H4",
    "H4": "H4",
    "1D": "D1",
    "D1": "D1",
}


def _decision_time(event: BrooksEvent) -> int:
    value = event.payload.get("decision_time_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("H1 event requires nonnegative decision_time_ms")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _timeframe_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _TIMEFRAME_ALIASES.get(value.strip().upper())


def _window_lists(value: Any) -> list[list[dict[str, Any]]]:
    if isinstance(value, Mapping):
        bars = value.get("bars")
        return [bars] if isinstance(bars, list) else []
    if isinstance(value, list) and (not value or isinstance(value[0], Mapping)):
        return [value]
    if isinstance(value, (list, tuple)):
        return [list(window) for window in value if isinstance(window, list)]
    return []


def _validate_references(
    intent: TradeIntentV2, windows: dict[str, Any]
) -> None:
    """Entry prices must exactly match a cited OHLC field in a read window."""
    if intent.decision == "NO_TRADE":
        if not intent.setup or not intent.setup.no_trade_reason:
            raise ValueError("NO_TRADE requires a specific no_trade_reason")
        return
    if (
        not intent.setup
        or intent.setup.trigger_status not in {"pending", "triggered", "present"}
        or intent.setup.location_assessment in {"poor", "unknown"}
        or intent.setup.no_trade_reason is not None
    ):
        raise ValueError("entry setup is not actionable")
    if intent.decision_timeframe != "M15":
        raise ValueError("production entry must cite M15 decision timeframe")
    if intent.trigger.source.timeframe != "M15" or intent.invalidation.source.timeframe != "M15":
        raise ValueError("production entry trigger and invalidation must cite M15 bars")
    if intent.setup.trigger_status == "pending" and intent.trigger:
        expected_direction = "above" if intent.decision == "ENTER_LONG" else "below"
        if intent.trigger.kind != "stop" or intent.trigger.direction != expected_direction:
            raise ValueError("pending trigger has invalid stop direction")
    for reference in (intent.trigger, intent.invalidation):
        assert reference is not None  # guaranteed by TradeIntentV2
        raw_windows = windows.get("M15", [])
        for bars in _window_lists(raw_windows):
            index = reference.source.bar_index
            if index >= len(bars):
                continue
            bar = bars[index]
            if (
                bar.get("open_time_ms") == reference.source.open_time_ms
                and bar.get("close_time_ms") == reference.source.close_time_ms
                and bar.get("close_time_ms")
                == bar.get("open_time_ms", -1) + _INTERVAL_MS["M15"] - 1
                and bar.get("close_time_ms", intent.decision_time_ms + 1)
                <= intent.decision_time_ms
                and bar.get("closed") is not False
                and _same_decimal_price(bar.get(reference.price_field), reference.price)
            ):
                break
        else:
            raise ValueError("entry reference does not match any supplied closed bar")


def _same_decimal_price(observed: Any, cited: str) -> bool:
    """Compare canonical decimal text by exact numeric value, never by tolerance."""
    if not isinstance(observed, str):
        return False
    try:
        return _decimal(observed) == _decimal(cited)
    except ValueError:
        return False


def _latest_closed_time(decision_time_ms: int, timeframe: str) -> int:
    interval = _INTERVAL_MS[timeframe]
    return ((decision_time_ms + 1) // interval) * interval - 1


def _valid_raw_window(bars: Any, timeframe: str, decision_time_ms: int) -> bool:
    if not isinstance(bars, list) or len(bars) < 120:
        return False
    interval = _INTERVAL_MS[timeframe]
    candidate = bars[-120:]
    for index, bar in enumerate(candidate):
        if not isinstance(bar, Mapping):
            return False
        try:
            open_ms = bar["open_time_ms"]
            close_ms = bar["close_time_ms"]
        except KeyError:
            return False
        if (
            isinstance(open_ms, bool)
            or isinstance(close_ms, bool)
            or not isinstance(open_ms, int)
            or not isinstance(close_ms, int)
            or open_ms < 0
            or close_ms != open_ms + interval - 1
            or close_ms > decision_time_ms
        ):
            return False
        if index and open_ms != candidate[index - 1]["open_time_ms"] + interval:
            return False
    if candidate[-1]["close_time_ms"] != _latest_closed_time(decision_time_ms, timeframe):
        return False
    return True


class _DurableToolAudit(list[dict[str, Any]]):
    """Mutable agent-run audit list that checkpoints every appended tool call."""

    def __init__(self, attempt: int, save: Callable[[list[dict[str, Any]]], None]):
        super().__init__()
        self.attempt = attempt
        self.save = save

    def append(self, value: dict[str, Any]) -> None:
        if isinstance(value, dict):
            value.setdefault("attempt", self.attempt)
        super().append(value)
        self.save(self)

    def persist(self) -> None:
        self.save(self)


@dataclass
class TraderConsumer:
    agent_key: str
    source: CandleSource
    store: BrooksStore
    events: EventBus
    user_id: int | None = None
    timeout_sec: float = 360
    shadow_mode: bool = True
    backend_key: str | None = None
    max_role_attempts: int = 2
    retry_backoff_sec: float = 5
    _cycle_store: Any | None = field(default=None, init=False, repr=False)
    _cycle_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_sec <= 0:
            raise ValueError("Trader timeout_sec must be positive")
        if isinstance(self.max_role_attempts, bool) or not 1 <= self.max_role_attempts <= 2:
            raise ValueError("max_role_attempts must be 1 or 2")
        if self.retry_backoff_sec < 0:
            raise ValueError("retry_backoff_sec must be nonnegative")
        root = getattr(self.store, "root", None)
        self._cycle_store = (
            DecisionCycleStore(Path(root)) if root is not None else MemoryDecisionCycleStore()
        )

    @property
    def _backend_resource(self) -> str:
        return self.backend_key or backend_resource_key(self.agent_key)

    def _load_macro_contexts(
        self, symbol: str, decision_time_ms: int
    ) -> dict[str, dict[str, Any]]:
        store = self.store
        if hasattr(store, "read_market_contexts"):
            raw_contexts = store.read_market_contexts(("D1", "H4"), symbol=symbol)
        else:
            raw_contexts = {
                label: store.read_market_context(label, symbol=symbol)
                if hasattr(store, "read_market_context")
                else None
                for label in ("D1", "H4")
            }
        result: dict[str, dict[str, Any]] = {}
        for timeframe in ("D1", "H4"):
            raw = raw_contexts.get(timeframe) if isinstance(raw_contexts, Mapping) else None
            context: MarketContextV2 | None = None
            if raw is not None:
                try:
                    context = MarketContextV2.model_validate(raw)
                except Exception:
                    log.warning("Ignoring invalid stored %s Brooks context for %s", timeframe, symbol)
                    context = None
            if context is not None and (
                context.symbol != symbol or context.timeframe != timeframe
            ):
                context = None
            if context is not None and context.decision_time_ms > decision_time_ms:
                # A future context is rejected from the frozen packet.
                log.warning("Ignoring future %s Brooks context for %s", timeframe, symbol)
                context = None
            if context is None:
                result[timeframe] = {"freshness": "missing", "context": None}
                continue
            expected_close = _latest_closed_time(decision_time_ms, timeframe)
            freshness = (
                "current"
                if context.window_bars == 120
                and context.decision_time_ms == expected_close
                else "stale"
            )
            result[timeframe] = {
                "freshness": freshness,
                "context": context.model_dump(mode="json"),
            }
        return result

    async def _build_packet(self, event: BrooksEvent, decision_time_ms: int) -> dict[str, Any]:
        market_tools = TraderMarketTools(
            source=self.source,
            decision_time_ms=decision_time_ms,
        )
        windows: dict[str, dict[str, Any]] = {}
        for label, interval in (("H1", "1h"), ("M15", "15m")):
            windows[label] = {
                "interval": interval,
                "bars": await market_tools.get_closed_candles(event.symbol, interval, 120),
            }
        if windows["H1"]["bars"][-1]["close_time_ms"] not in {
            decision_time_ms,
            decision_time_ms - 1,
        }:
            raise ValueError("H1 close event lacks its closed decision bar")
        return {
            "schema": "brooks.trader-market-input.v2",
            "role": "TRADER",
            "symbol": event.symbol,
            "decision_time_ms": decision_time_ms,
            "macro_contexts": self._load_macro_contexts(event.symbol, decision_time_ms),
            "raw": {
                "H1": windows["H1"]["bars"],
                "M15": windows["M15"]["bars"],
            },
        }

    def _intent_event_exists(self, event_id: str) -> bool:
        reader = getattr(self.store, "read_events", None)
        if callable(reader):
            try:
                return any(row.event_id == event_id for row in reader())
            except Exception as exc:
                log.exception("Could not check persisted Brooks events for idempotency")
                raise RuntimeError("could not verify Brooks event idempotency") from exc
        return False

    def _intent_already_saved(self, intent: TradeIntentV2) -> bool:
        expected = (intent.symbol, intent.decision_time_ms)
        latest = None
        try:
            latest = self.store.read_latest("trader")
        except FileNotFoundError:
            latest = None
        if isinstance(latest, Mapping) and (
            latest.get("symbol"), latest.get("decision_time_ms")
        ) == expected:
            return True
        root = getattr(self.store, "root", None)
        if root is not None:
            history = Path(root) / "trader" / "history.jsonl"
            try:
                for line in history.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if (row.get("symbol"), row.get("decision_time_ms")) == expected:
                        return True
            except FileNotFoundError:
                return False
        return False

    def _record_tool_audit(
        self,
        cycle_id: str,
        status: str,
        attempt: int,
        all_audits: list[dict[str, Any]],
        attempt_audit: list[dict[str, Any]],
    ) -> None:
        # Keep tool names, safe JSON arguments and status only. Never persist tool output.
        rows: list[dict[str, Any]] = []
        for source in (*all_audits, *attempt_audit):
            clean: dict[str, Any] = {}
            for key in ("role", "tool", "name", "arguments", "status", "error_type", "result_type", "result_count", "attempt"):
                if key in source:
                    clean[key] = source[key]
            if not clean:
                continue
            clean.setdefault("attempt", attempt)
            rows.append(clean)
        current = self._cycle_store.get(cycle_id) or {}
        self._cycle_store.update(
            cycle_id,
            status=status,
            updated_at_ms=time.time_ns() // 1_000_000,
            tool_audit=rows,
            attempt=attempt,
            packet_hash=current.get("packet_hash"),
        )

    async def _publish_failure(
        self,
        event: BrooksEvent,
        cycle_id: str,
        decision_time_ms: int,
        attempt: int,
        error: BaseException,
        *,
        transient: bool,
    ) -> None:
        failure = {
            "error_type": type(error).__name__,
            "message": str(error)[:800],
            "attempt": attempt,
            "transient": transient,
        }
        current = self._cycle_store.get(cycle_id) or {}
        failed_id = str(uuid5(NAMESPACE_URL, f"brooks-trader-failed:{cycle_id}"))
        self._cycle_store.update(
            cycle_id,
            status="failed",
            updated_at_ms=time.time_ns() // 1_000_000,
            attempt=attempt,
            failure=failure,
            packet_hash=current.get("packet_hash"),
            failure_event_id=failed_id,
            failure_event_published=False,
        )
        await self._ensure_failure_event(event, cycle_id, decision_time_ms)

    async def _ensure_failure_event(
        self, event: BrooksEvent, cycle_id: str, decision_time_ms: int
    ) -> None:
        current = self._cycle_store.get(cycle_id) or {}
        failed_id = current.get("failure_event_id") or str(
            uuid5(NAMESPACE_URL, f"brooks-trader-failed:{cycle_id}")
        )
        failure = current.get("failure") or {}
        if current.get("failure_event_published") or self._intent_event_exists(failed_id):
            self._cycle_store.update(
                cycle_id,
                status="failed",
                updated_at_ms=time.time_ns() // 1_000_000,
                failure_event_id=failed_id,
                failure_event_published=True,
            )
            return
        await self.events.publish(
            BrooksEvent(
                type=EventType.TRADER_DECISION_FAILED,
                symbol=event.symbol,
                correlation_id=event.correlation_id or cycle_id,
                causation_id=event.event_id,
                event_id=failed_id,
                payload={
                    "cycle_id": cycle_id,
                    "decision_time_ms": decision_time_ms,
                    "role": "TRADER",
                    "attempt": failure.get("attempt", 0),
                    "error_type": failure.get("error_type", "RoleRunError"),
                    "message": failure.get("message", "Trader role failed"),
                    "transient": failure.get("transient", False),
                },
            )
        )
        self._cycle_store.update(
            cycle_id,
            status="failed",
            updated_at_ms=time.time_ns() // 1_000_000,
            failure_event_id=failed_id,
            failure_event_published=True,
        )

    async def _complete_intent(
        self,
        event: BrooksEvent,
        cycle_id: str,
        intent: TradeIntentV2,
        attempt: int,
    ) -> TradeIntentV2:
        event_id = str(uuid5(NAMESPACE_URL, f"brooks-trader-intent:{cycle_id}"))
        current = self._cycle_store.get(cycle_id) or {}
        has_saved_candidate = bool(current.get("intent"))
        if not has_saved_candidate or current.get("event_id") != event_id:
            self._cycle_store.update(
                cycle_id,
                status=current.get("status", "running"),
                updated_at_ms=time.time_ns() // 1_000_000,
                attempt=attempt,
                intent=intent.model_dump(mode="json"),
                event_id=event_id,
            )
        if not self._intent_already_saved(intent):
            self.store.save_trader_intent(intent.model_dump(mode="json"))
        if not has_saved_candidate or not self._intent_event_exists(event_id):
            await self.events.publish(
                BrooksEvent(
                    type=EventType.TRADER_INTENT_CREATED,
                    symbol=event.symbol,
                    correlation_id=event.correlation_id or cycle_id,
                    causation_id=event.event_id,
                    event_id=event_id,
                    payload={
                        "intent": intent.model_dump(mode="json"),
                        "shadow_mode": self.shadow_mode,
                    },
                )
            )
        self._cycle_store.update(
            cycle_id,
            status="completed",
            updated_at_ms=time.time_ns() // 1_000_000,
            attempt=attempt,
            intent=intent.model_dump(mode="json"),
            event_id=event_id,
            failure=None,
        )
        return intent

    async def _resume_saved_intent(
        self,
        event: BrooksEvent,
        cycle_id: str,
        decision_time_ms: int,
        record: Mapping[str, Any],
    ) -> TradeIntentV2:
        intent = TradeIntentV2.model_validate(record["intent"])
        return await self._complete_intent(
            event, cycle_id, intent, int(record.get("attempt", 0))
        )

    async def handle(self, event: BrooksEvent) -> TradeIntentV2 | None:
        if event.type != EventType.H1_BAR_CLOSED:
            raise ValueError("Trader only handles H1_BAR_CLOSED")
        decision_time_ms = _decision_time(event)
        cycle_id = DecisionCycleStore.identity(event.symbol, decision_time_ms)
        lock = self._cycle_locks.setdefault(cycle_id, asyncio.Lock())
        async with lock:
            cycle_id, record = self._cycle_store.create_pending(
                symbol=event.symbol,
                decision_time_ms=decision_time_ms,
                created_at_ms=time.time_ns() // 1_000_000,
            )
            if record.get("status") == "completed" and record.get("intent"):
                return TradeIntentV2.model_validate(record["intent"])
            if record.get("status") == "failed":
                await self._ensure_failure_event(event, cycle_id, decision_time_ms)
                return None
            if record.get("intent"):
                return await self._resume_saved_intent(
                    event, cycle_id, decision_time_ms, record
                )

            try:
                packet = record.get("frozen_packet")
                if packet is None:
                    packet = await self._build_packet(event, decision_time_ms)
                    encoded = _canonical_json(packet)
                    packet = json.loads(encoded)
                    packet_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                    self._cycle_store.update(
                        cycle_id,
                        status="pending",
                        updated_at_ms=time.time_ns() // 1_000_000,
                        packet_hash=packet_hash,
                        frozen_packet=packet,
                    )
                else:
                    if (
                        packet.get("symbol") != event.symbol
                        or packet.get("decision_time_ms") != decision_time_ms
                    ):
                        raise ValueError("persisted Trader snapshot identity mismatch")
                    encoded = _canonical_json(packet)
                    packet_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                    if record.get("packet_hash") != packet_hash:
                        raise ValueError("persisted Trader snapshot hash mismatch")
            except Exception as exc:
                await self._publish_failure(
                    event,
                    cycle_id,
                    decision_time_ms,
                    int(record.get("attempt", 0)),
                    exc,
                    transient=is_transient_role_error(exc),
                )
                return None

            attempt_audit_all: list[dict[str, Any]] = list(record.get("tool_audit") or [])
            attempt = max(1, int(record.get("attempt", 0)) + 1)
            # A process restart can resume an interrupted frozen round once.
            if attempt > self.max_role_attempts:
                error = TimeoutError("Trader process restarted after final role attempt")
                await self._publish_failure(
                    event,
                    cycle_id,
                    decision_time_ms,
                    int(record.get("attempt", 0)),
                    error,
                    transient=True,
                )
                return None

            cached_reads: dict[str, Any] = {}
            async with backend_call_slot(self._backend_resource, priority=0):
                while attempt <= self.max_role_attempts:
                    cycle_status = "running" if attempt == 1 else "retrying"
                    self._cycle_store.update(
                        cycle_id,
                        status=cycle_status,
                        updated_at_ms=time.time_ns() // 1_000_000,
                        attempt=attempt,
                        packet_hash=packet_hash,
                        frozen_packet=packet,
                        tool_audit=attempt_audit_all,
                    )
                    # Each attempt receives a new object reconstructed from the
                    # same canonical snapshot, so a client cannot mutate a retry.
                    role_packet = json.loads(encoded)
                    h4_verified = False
                    observed_windows: dict[str, list[list[dict[str, Any]]]] = {
                        "H1": [role_packet["raw"]["H1"]],
                        "M15": [role_packet["raw"]["M15"]],
                        "H4": [],
                        "D1": [],
                    }
                    audit_status = cycle_status
                    def set_h4_verified(value: bool) -> None:
                        nonlocal h4_verified
                        h4_verified = value

                    def persist_audit(rows: list[dict[str, Any]]) -> None:
                        self._record_tool_audit(
                            cycle_id,
                            audit_status,
                            attempt,
                            attempt_audit_all,
                            rows,
                        )

                    durable_audit = _DurableToolAudit(attempt, persist_audit)
                    monitored_tools = self._monitored_tools(
                        event.symbol,
                        decision_time_ms,
                        cached_reads,
                        observed_windows,
                        set_h4_verified,
                    )
                    try:
                        intent = await run_role(
                            "TRADER",
                            role_packet,
                            TradeIntentV2,
                            monitored_tools,
                            agent_key=self.agent_key,
                            backend_key=self._backend_resource,
                            priority=0,
                            timeout_sec=self.timeout_sec,
                            user_id=self.user_id,
                            tool_audit=durable_audit,
                            output_validator=lambda candidate: _validate_references(
                                candidate, observed_windows
                            ),
                        )
                        durable_audit.persist()
                        if intent.symbol != event.symbol or intent.decision_time_ms != decision_time_ms:
                            raise ValueError("Trader output symbol or decision time mismatch")
                        if not {"H1", "M15"}.issubset(intent.context_timeframes_used):
                            raise ValueError("Trader output lacks H1/M15 timeframe coverage")
                        h4_freshness = role_packet["macro_contexts"]["H4"]["freshness"]
                        if (
                            intent.decision in {"ENTER_LONG", "ENTER_SHORT"}
                            and h4_freshness in {"stale", "missing"}
                            and not h4_verified
                        ):
                            raise ValueError(
                                "entry with stale or missing H4 context requires a successful raw H4 read in this role attempt"
                            )
                        if intent.decision in {"ENTER_LONG", "ENTER_SHORT"} and "H4" not in intent.context_timeframes_used:
                            raise ValueError("entry output must include H4 among context_timeframes_used")
                        _validate_references(intent, observed_windows)
                    except Exception as exc:
                        durable_audit.persist()
                        attempt_audit_all.extend(deepcopy(durable_audit))
                        self._record_tool_audit(
                            cycle_id,
                            audit_status,
                            attempt,
                            attempt_audit_all,
                            [],
                        )
                        transient = is_transient_role_error(exc)
                        if transient and attempt < self.max_role_attempts:
                            attempt += 1
                            self._cycle_store.update(
                                cycle_id,
                                status="retrying",
                                updated_at_ms=time.time_ns() // 1_000_000,
                                attempt=attempt,
                                packet_hash=packet_hash,
                                frozen_packet=packet,
                                tool_audit=attempt_audit_all,
                                failure={
                                    "error_type": type(exc).__name__,
                                    "message": str(exc)[:800],
                                    "attempt": attempt - 1,
                                    "transient": True,
                                },
                            )
                            await asyncio.sleep(self.retry_backoff_sec)
                            continue
                        await self._publish_failure(
                            event,
                            cycle_id,
                            decision_time_ms,
                            attempt,
                            exc,
                            transient=transient,
                        )
                        return None
                    attempt_audit_all.extend(deepcopy(durable_audit))
                    self._record_tool_audit(
                        cycle_id,
                        audit_status,
                        attempt,
                        attempt_audit_all,
                        [],
                    )
                    return await self._complete_intent(
                        event, cycle_id, intent, attempt
                    )
            return None

    def _monitored_tools(
        self,
        symbol: str,
        decision_time_ms: int,
        cache: dict[str, Any],
        observed_windows: dict[str, list[list[dict[str, Any]]]],
        set_h4_verified: Callable[[bool], None],
    ) -> dict[str, Callable[..., Any]]:
        base = TraderMarketTools(source=self.source, decision_time_ms=decision_time_ms)
        bound = bind_symbol_tools(
            symbol,
            {
                "get_closed_candles": base.get_closed_candles,
                "get_recent_structure": base.get_recent_structure,
                "get_volatility": base.get_volatility,
            },
        )
        tools: dict[str, Callable[..., Any]] = {}
        for name, fn in bound.items():

            async def monitored(*args: Any, _name=name, _fn=fn, **kwargs: Any) -> Any:
                cache_key = _canonical_json([_name, args, kwargs])
                if cache_key in cache:
                    value = deepcopy(cache[cache_key])
                else:
                    value = _fn(*args, **kwargs)
                    if hasattr(value, "__await__"):
                        value = await value
                    cache[cache_key] = deepcopy(value)
                timeframe = _timeframe_name(kwargs.get("timeframe"))
                if _name == "get_closed_candles" and timeframe in {"M15", "H4", "D1"}:
                    bars = value if isinstance(value, list) else []
                    observed_windows.setdefault(timeframe, []).append(bars)
                    if (
                        timeframe == "H4"
                        and isinstance(kwargs.get("limit"), int)
                        and not isinstance(kwargs.get("limit"), bool)
                        and kwargs["limit"] >= 120
                        and _valid_raw_window(bars, "H4", decision_time_ms)
                    ):
                        set_h4_verified(True)
                return value

            tools[name] = monitored
        return tools

    async def run(self) -> None:
        queue = self.events.subscribe([EventType.H1_BAR_CLOSED])
        try:
            while True:
                event = await queue.get()
                try:
                    await self.handle(event)
                except Exception:
                    log.exception(
                        "Brooks Trader failed for %s event %s",
                        event.symbol,
                        event.event_id,
                    )
        finally:
            self.events.unsubscribe(queue)
