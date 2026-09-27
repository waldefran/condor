"""Drift-free closed-bar clock for the independent Brooks lifecycle.

The clock owns no trading, model, or venue-write surface. On every absolute
bar close (plus the configured wake offset) it fetches a minimal closed window
through :class:`ClosedBarGate` and publishes exactly one ``H1_BAR_CLOSED`` /
``D1_BAR_CLOSED`` event per symbol. Forming bars, future bars, gaps, and short
history fail closed: nothing is published and the miss is logged, never
retried as a partial bar.

Scheduling never sleeps a fixed period as a candle clock (no ``sleep(3600)``).
Each iteration recomputes the latest due close from the absolute epoch grid,
so restarts, pauses, and slow publishes cannot accumulate drift: consecutive
wakes for one timeframe are always exactly one interval apart.

Integration seams (all injected; production Condor/Hummingbot adapters stay
behind these narrow callables and are owned by other worktrees):

- ``source``: ``CandleSource`` (async ``fetch_candles(symbol, timeframe,
  limit)``) or a plain async callable with the same shape. Read-only.
- ``publish``: an ``EventBus`` (has ``.publish``) or a plain async callable
  taking the event envelope. The bus persists before fan-out.
- ``now_fn``: ``() -> int`` epoch milliseconds. Defaults to wall time.
- ``sleep_fn``: ``async (delay_sec: float) -> None``. Defaults to a
  stop-aware wait. Tests inject a fake that advances a fake clock.
- ``stop`` / ``wait_resumed``: supplied to :meth:`MarketClock.run` by
  ``BrooksSupervisor`` so pause/resume is honoured at safe points.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import MarketWakeConfig
from .events import BrooksEvent, EventBus, EventType
from .market_tools import INTERVAL_MS, TIMEFRAME_ALIASES

log = logging.getLogger(__name__)

NowFn = Callable[[], int]
SleepFn = Callable[[float], Awaitable[None]]

_CLOCK_FETCH_LIMIT = 2
_CLOCK_REQUIRED_BARS = 1
_CLOCK_BACKOFF_SEC = 5.0


def canonical_timeframe(timeframe: str) -> str:
    resolved = TIMEFRAME_ALIASES.get(timeframe, timeframe)
    if resolved not in INTERVAL_MS:
        raise ValueError(f"unsupported Brooks clock timeframe: {timeframe!r}")
    return resolved


def timeframe_ms(timeframe: str) -> int:
    return INTERVAL_MS[canonical_timeframe(timeframe)]


def latest_due_close(now_ms: int, interval_ms: int, offset_ms: int) -> int:
    """Latest bar close (inclusive-end ms) whose wake time has passed.

    Bars close at ``k * interval - 1`` and wake at ``close + offset``.
    """
    if now_ms < 0 or interval_ms <= 0 or offset_ms < 0:
        raise ValueError("clock requires nonnegative time and positive interval")
    slot = (now_ms - offset_ms + 1) // interval_ms
    return slot * interval_ms - 1


def _default_now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class MarketClock:
    """Publish H1/D1 closed-bar events at close_time + wake offset."""

    symbols: list[str] = field(default_factory=list)
    trader: MarketWakeConfig = field(
        default_factory=lambda: MarketWakeConfig(
            timeframe="1h", wake_offset_sec=2
        )
    )
    htf: MarketWakeConfig = field(
        default_factory=lambda: MarketWakeConfig(timeframe="1d", wake_offset_sec=3)
    )
    source: Any | None = None
    publish: Any | None = None
    now_fn: NowFn = _default_now_ms
    sleep_fn: SleepFn | None = None

    def _fetch(self, symbol: str, timeframe: str, limit: int) -> Awaitable[Any]:
        fetch = getattr(self.source, "fetch_candles", self.source)
        return fetch(symbol, timeframe, limit)

    async def _sleep(self, delay_sec: float, stop: asyncio.Event) -> None:
        if delay_sec <= 0:
            await asyncio.sleep(0)
            return
        if self.sleep_fn is not None:
            await self.sleep_fn(delay_sec)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay_sec)
        except TimeoutError:
            pass

    async def publish_closed_bar(
        self,
        symbol: str,
        timeframe: str,
        decision_time_ms: int,
        event_type: EventType,
    ) -> BrooksEvent:
        """Gate one closed bar and publish its event; raise instead of partial."""
        from .market_tools import ClosedBarGate

        if self.source is None or self.publish is None:
            raise RuntimeError("MarketClock requires a candle source and publisher")
        if not symbol:
            raise ValueError("symbol is required")
        canonical = canonical_timeframe(timeframe)
        gate = ClosedBarGate(timeframe=canonical, decision_time_ms=decision_time_ms)
        raw = await self._fetch(symbol, canonical, _CLOCK_FETCH_LIMIT)
        bars = gate.validate(raw, required_count=_CLOCK_REQUIRED_BARS, trigger_timeframe=True)
        event = BrooksEvent(
            type=event_type,
            symbol=symbol,
            payload={
                "decision_time_ms": decision_time_ms,
                "close_time_ms": decision_time_ms,
                "timeframe": canonical,
                "bar": bars[-1].model_dump(exclude_none=True),
            },
            correlation_id=f"{symbol}-{canonical}-{decision_time_ms}",
        )
        callback = getattr(self.publish, "publish", self.publish)
        result = callback(event)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            await result
        return event

    def _schedules(self) -> list[tuple[str, int, int, EventType, MarketWakeConfig]]:
        return [
            ("trader", timeframe_ms(self.trader.timeframe), self.trader.wake_offset_sec * 1000, EventType.H1_BAR_CLOSED, self.trader),
            ("htf", timeframe_ms(self.htf.timeframe), self.htf.wake_offset_sec * 1000, EventType.D1_BAR_CLOSED, self.htf),
        ]

    async def run(
        self,
        stop: asyncio.Event,
        wait_resumed: Callable[[], Awaitable[None]],
    ) -> None:
        """Run until ``stop`` is set; wait out pauses at the top of each tick."""
        if self.source is None or not self.symbols or self.publish is None:
            log.info("Brooks MarketClock idle: no candle source, symbols, or bus")
            await stop.wait()
            return
        last_published: dict[str, int] = {}
        while not stop.is_set():
            await wait_resumed()
            if stop.is_set():
                break
            now_ms = self.now_fn()
            delays: list[float] = []
            try:
                for key, interval_ms, offset_ms, event_type, wake in self._schedules():
                    canonical = canonical_timeframe(wake.timeframe)
                    last = last_published.get(key)
                    if last is None:
                        due = latest_due_close(now_ms, interval_ms, offset_ms)
                        if due >= 0 and due + offset_ms <= now_ms:
                            for symbol in self.symbols:
                                await self.publish_closed_bar(
                                    symbol, canonical, due, event_type
                                )
                            last_published[key] = due
                            continue
                        delays.append((due + offset_ms - now_ms) / 1000)
                    else:
                        nxt = last + interval_ms
                        if nxt + offset_ms <= now_ms:
                            for symbol in self.symbols:
                                await self.publish_closed_bar(
                                    symbol, canonical, nxt, event_type
                                )
                            last_published[key] = nxt
                            continue
                        delays.append((nxt + offset_ms - now_ms) / 1000)
            except Exception:
                log.exception("Brooks MarketClock publish failed; backing off")
                await self._sleep(_CLOCK_BACKOFF_SEC, stop)
                continue
            if delays:
                delay = min(delays)
                if delay > 0:
                    await self._sleep(delay, stop)
            else:
                await self._sleep(0, stop)
