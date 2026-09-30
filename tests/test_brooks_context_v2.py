from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from condor.brooks.clock import MarketClock, latest_due_close
from condor.brooks.config import MarketWakeConfig
from condor.brooks.contracts import MarketContextV2
from condor.brooks.events import BrooksEvent, EventType
from condor.brooks.market_tools import ClosedBarError
from condor.brooks.store import BrooksStore

H1 = 3_600_000
H4 = 4 * H1
D1 = 24 * H1
SYMBOL = "BTC-USDT"


def _bars(interval: int, end: int, count: int, *, closed: bool = True):
    first_open = end - interval + 1 - (count - 1) * interval
    result = []
    for index in range(count):
        opened = first_open + index * interval
        price = 100 + index
        result.append(
            {
                "open_time_ms": opened,
                "close_time_ms": opened + interval - 1,
                "open": str(price),
                "high": str(price + 2),
                "low": str(price - 1),
                "close": str(price + 1),
                "volume": "10",
                "closed": closed,
            }
        )
    return result


def _context(timeframe: str, decision: int, *, window_bars: int = 120):
    return MarketContextV2.model_validate(
        {
            "schema": "brooks.market-context.v2",
            "role": "CONTEXT_ANALYST",
            "symbol": SYMBOL,
            "timeframe": timeframe,
            "decision_time_ms": decision,
            "window_bars": window_bars,
            "primary_regime": "transition-unclear",
            "phase": "unclear",
            "breakout_mode": "unclear",
            "directional_pressure": "balanced",
            "always_in": "unclear",
            "always_in_relevance": "low",
            "observations": ["Recent bars overlap and trade both ways."],
            "structures": [
                {"kind": "two-sided", "description": "Alternating legs and tails."}
            ],
            "evidence_for": ["Repeated overlap supports two-sided trade."],
            "evidence_against": ["The last two closes move in one direction."],
            "transition_conditions": ["Follow-through beyond the recent boundary."],
            "missing_information": [],
            "confidence": "low",
        }
    )


def test_market_context_v2_rejects_trade_recommendation_and_private_fields():
    valid = _context("H4", H4 - 1).model_dump(mode="json")
    with pytest.raises(ValidationError):
        MarketContextV2.model_validate({**valid, "recommended_entry": "100"})
    with pytest.raises(ValidationError):
        MarketContextV2.model_validate({**valid, "position": {"side": "LONG"}})
    with pytest.raises(ValidationError):
        MarketContextV2.model_validate({**valid, "probability_up": 0.7})
    with pytest.raises(ValidationError, match="decision_time_ms"):
        MarketContextV2.model_validate(
            {
                **valid,
                "structures": [
                    {
                        "kind": "range",
                        "description": "Future boundary probe.",
                        "end_time_ms": H4,
                    }
                ],
            }
        )


def test_context_store_separates_timeframes_recovers_and_reads_legacy_v1(tmp_path):
    store = BrooksStore(tmp_path)
    d1 = _context("D1", D1 - 1)
    h4 = _context("H4", H4 - 1)
    store.save_market_context(d1)
    store.save_market_context(h4)

    d1_latest = store.root / "context" / "d1" / "latest.json"
    d1_latest.write_text("broken", encoding="utf-8")
    reopened = BrooksStore(tmp_path)
    assert reopened.read_market_context("D1", symbol=SYMBOL) == d1.model_dump(mode="json")
    assert reopened.read_market_context("H4", symbol=SYMBOL) == h4.model_dump(mode="json")
    assert reopened.read_market_contexts(symbol=SYMBOL).keys() == {"D1", "H4"}

    legacy = {
        "schema": "brooks.market-context.v1",
        "role": "HTF_ANALYST",
        "symbol": SYMBOL,
        "decision_time_ms": D1,
        "timeframe": "D1",
        "observations": ["Legacy bar window was two-sided."],
        "evidence_against": ["Legacy context omitted an axis."],
        "uncertainty": ["The old summary did not record its window size."],
    }
    legacy_store = BrooksStore(tmp_path / "legacy")
    legacy_store.save_market_context(legacy)
    legacy_view = legacy_store.read_market_context("D1", symbol=SYMBOL)
    assert legacy_view["schema"] == "brooks.market-context.v2"
    assert legacy_view["window_bars"] == 0
    assert legacy_view["confidence"] == "low"
    assert legacy_view["missing_information"]
    assert legacy_store.read_latest("htf") == legacy


def test_pm_read_boundary_retains_d1_context_after_v2_store_migration(tmp_path):
    from condor.brooks.adapters import _pm_latest_context

    context = _context("D1", D1 - 1)
    BrooksStore(tmp_path).save_market_context(context)
    pm_view = _pm_latest_context(tmp_path, SYMBOL)
    assert pm_view["schema"] == "brooks.market-context.v1"
    assert pm_view["decision_time_ms"] == context.decision_time_ms
    assert pm_view["observations"] == context.observations


class _Source:
    def __init__(self, bars_by_tf):
        self.bars_by_tf = bars_by_tf
        self.calls = []

    async def fetch_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return [dict(bar) for bar in self.bars_by_tf[timeframe][-limit:]]


class _EventSink:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)


@pytest.mark.parametrize(
    ("timeframe", "event_type", "interval", "label"),
    [
        ("1d", EventType.D1_BAR_CLOSED, D1, "D1"),
        ("4h", EventType.H4_BAR_CLOSED, H4, "H4"),
    ],
)
@pytest.mark.asyncio
async def test_context_analyst_uses_exactly_120_closed_bars(
    timeframe, event_type, interval, label, monkeypatch, tmp_path
):
    from condor.brooks import htf_analyst

    decision = 121 * D1 - 1 if timeframe == "1d" else 121 * H4 - 1
    source = _Source({timeframe: _bars(interval, decision, 120)})
    store = BrooksStore(tmp_path)
    sink = _EventSink()
    output = _context(label, decision)
    seen = {}

    async def fake_role(role, prompt, output_model, market_tools, **kwargs):
        seen.update(role=role, prompt=prompt, output_model=output_model, tools=market_tools, kwargs=kwargs)
        assert len(prompt["bars"]) == 120
        return output

    monkeypatch.setattr(htf_analyst, "run_coordinated_role", fake_role)
    consumer = htf_analyst.ContextAnalystConsumer(
        agent_key="openai:test",
        source=source,
        store=store,
        events=sink,
        timeframe=timeframe,
        backend_key="openai",
    )
    result = await consumer.handle(
        BrooksEvent(event_type, SYMBOL, {"decision_time_ms": decision})
    )
    assert result == output
    assert seen["role"] == "CONTEXT_ANALYST"
    assert seen["output_model"] is MarketContextV2
    assert seen["prompt"]["timeframe"] == timeframe
    assert "get_market_context" not in seen["tools"]
    assert "account" not in json.dumps(seen["prompt"]).lower()
    assert source.calls == [(SYMBOL, timeframe, 121)]
    assert store.read_market_context(label, symbol=SYMBOL) == output.model_dump(mode="json")
    assert sink.published[-1].type == EventType.MARKET_CONTEXT_UPDATED


@pytest.mark.parametrize(
    ("timeframe", "event_type", "interval", "label"),
    [
        ("1d", EventType.D1_BAR_CLOSED, D1, "D1"),
        ("4h", EventType.H4_BAR_CLOSED, H4, "H4"),
    ],
)
@pytest.mark.asyncio
async def test_context_analyst_deduplicates_concurrent_and_persisted_decision(
    timeframe, event_type, interval, label, monkeypatch, tmp_path
):
    from condor.brooks import htf_analyst

    decision = 122 * interval - 1
    source = _Source({timeframe: _bars(interval, decision, 120)})
    store = BrooksStore(tmp_path)
    sink = _EventSink()
    output = _context(label, decision)
    role_calls = 0
    role_started = asyncio.Event()
    finish_role = asyncio.Event()

    async def fake_role(role, prompt, output_model, market_tools, **kwargs):
        nonlocal role_calls
        role_calls += 1
        role_started.set()
        await finish_role.wait()
        return output

    monkeypatch.setattr(htf_analyst, "run_coordinated_role", fake_role)
    consumer = htf_analyst.ContextAnalystConsumer(
        "openai:test", source, store, sink, timeframe=timeframe
    )
    event = BrooksEvent(event_type, SYMBOL, {"decision_time_ms": decision})

    first = asyncio.create_task(consumer.handle(event))
    await role_started.wait()
    second_started = asyncio.Event()

    async def duplicate():
        second_started.set()
        return await consumer.handle(
            BrooksEvent(event_type, SYMBOL, {"decision_time_ms": decision})
        )

    second = asyncio.create_task(duplicate())
    await second_started.wait()
    await asyncio.sleep(0)
    finish_role.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result == output
    assert role_calls == 1
    assert source.calls == [(SYMBOL, timeframe, 121)]
    assert [event.type for event in sink.published] == [
        EventType.MARKET_CONTEXT_UPDATED
    ]
    history = store.root / "context" / label.lower() / "history.jsonl"
    assert len(BrooksStore.read_jsonl(history)) == 1

    # A new consumer instance simulates process restart; the durable exact
    # decision must suppress candle reads, inference, and another update event.
    reopened = BrooksStore(tmp_path)
    restarted = htf_analyst.ContextAnalystConsumer(
        "openai:test", source, reopened, sink, timeframe=timeframe
    )
    persisted_result = await restarted.handle(
        BrooksEvent(event_type, SYMBOL, {"decision_time_ms": decision})
    )
    assert persisted_result == output
    assert role_calls == 1
    assert source.calls == [(SYMBOL, timeframe, 121)]
    assert len(sink.published) == 1

    # A newer stored context is not a cache hit for an older event.
    newer_store = BrooksStore(tmp_path / "newer")
    newer_store.save_market_context(_context(label, decision + interval))
    older_source = _Source({timeframe: _bars(interval, decision, 120)})
    older_consumer = htf_analyst.ContextAnalystConsumer(
        "openai:test",
        older_source,
        newer_store,
        _EventSink(),
        timeframe=timeframe,
    )
    older_result = await older_consumer.handle(event)
    assert older_result.decision_time_ms == decision
    assert role_calls == 2
    assert older_source.calls == [(SYMBOL, timeframe, 121)]


@pytest.mark.parametrize("forming", [False, True])
@pytest.mark.asyncio
async def test_context_analyst_rejects_future_only_or_forming_window(
    forming, monkeypatch, tmp_path
):
    from condor.brooks import htf_analyst

    decision = 121 * H4 - 1
    bars = _bars(H4, decision, 120)
    if forming:
        bars[-1]["closed"] = False
    else:
        # Only 119 bars are closed as of decision_time; the last is future.
        bars = bars[:-1] + _bars(H4, decision + H4, 1)
    source = _Source({"4h": bars})

    async def should_not_run(*args, **kwargs):
        raise AssertionError("LLM must not receive an invalid context window")

    monkeypatch.setattr(htf_analyst, "run_coordinated_role", should_not_run)
    consumer = htf_analyst.ContextAnalystConsumer(
        "openai:test", source, BrooksStore(tmp_path), _EventSink(), timeframe="4h"
    )
    with pytest.raises(ClosedBarError):
        await consumer.handle(
            BrooksEvent(EventType.H4_BAR_CLOSED, SYMBOL, {"decision_time_ms": decision})
        )


@pytest.mark.asyncio
async def test_clock_publishes_h4_and_h4_failure_does_not_block_h1_or_d1():
    decision = 12 * D1 - 1
    published = []

    class MixedSource:
        async def fetch_candles(self, symbol, timeframe, limit):
            if timeframe == "4h":
                raise RuntimeError("H4 transport unavailable")
            interval = {"1h": H1, "1d": D1}[timeframe]
            offset = 2_000 if timeframe == "1h" else 3_000
            close = latest_due_close(decision + offset, interval, offset)
            return _bars(interval, close, 2)

    async def end_after_error_sleep(_delay):
        stopped.set()

    clock = MarketClock(
        symbols=[SYMBOL], source=MixedSource(), publish=published.append,
        now_fn=lambda: decision + 10_000, sleep_fn=end_after_error_sleep,
        h4=MarketWakeConfig(timeframe="4h", wake_offset_sec=3),
    )
    stopped = asyncio.Event()
    resumed = asyncio.Event()
    resumed.set()
    await clock.run(stopped, resumed.wait)
    types = [event.type for event in published]
    assert EventType.H1_BAR_CLOSED in types
    assert EventType.D1_BAR_CLOSED in types
    assert EventType.H4_BAR_CLOSED not in types
