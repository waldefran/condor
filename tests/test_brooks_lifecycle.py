"""Independent Brooks lifecycle: clock, consumers, gating, GM routing, control."""

from __future__ import annotations

import asyncio

import pytest

from condor.brooks.clock import MarketClock, latest_due_close, timeframe_ms
from condor.brooks.config import BrooksConfig
from condor.brooks.events import BrooksEvent, EventType
from condor.brooks.gm import GMRejected
from condor.brooks.market_tools import ClosedBarError
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.supervisor import BrooksSupervisor, GMConsumer

SYMBOL = "BTC-USDT"
H1 = 3_600_000
D1 = 86_400_000


def closed_bars(interval_ms, end_close_ms, n, base=100):
    bars = []
    first_open = end_close_ms - interval_ms + 1 - (n - 1) * interval_ms
    for i in range(n):
        opened = first_open + i * interval_ms
        price = base + i
        bars.append(
            {
                "open_time_ms": opened,
                "close_time_ms": opened + interval_ms - 1,
                "open": str(price),
                "high": str(price + 2),
                "low": str(price - 1),
                "close": str(price + 1),
                "volume": "10",
                "closed": True,
            }
        )
    assert bars[-1]["close_time_ms"] == end_close_ms
    return bars


class FakeSource:
    def __init__(self, bars_by_timeframe):
        self.bars = bars_by_timeframe
        self.calls = []

    async def fetch_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return [dict(bar) for bar in self.bars[timeframe][-limit:]]


async def wait_for(queue, timeout=5.0):
    return await asyncio.wait_for(queue.get(), timeout)


async def collect_until(queue, predicate, timeout=5.0):
    async def _drain():
        while True:
            event = await queue.get()
            if predicate(event):
                return event

    return await asyncio.wait_for(_drain(), timeout)


def h1_decision():
    return 172_800_000 * 200 - 1


def trader_intent_decision(decision_time_ms, m15):
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "ENTER_LONG",
        "symbol": SYMBOL,
        "decision_time_ms": decision_time_ms,
        "market_context": None,
        "setup": {
            "type": "breakout",
            "trigger_status": "present",
            "signal_quality": "clear",
            "location_assessment": "favorable",
        },
        "decision_timeframe": "M15",
        "context_timeframes_used": ["H4", "H1", "M15"],
        "entry_mechanism": "breakout",
        "trigger": {
            "reference": "m15-breakout",
            "price_field": "close",
            "price": m15[5]["close"],
            "source": {
                "timeframe": "M15",
                "bar_index": 5,
                "open_time_ms": m15[5]["open_time_ms"],
                "close_time_ms": m15[5]["close_time_ms"],
            },
            "kind": "stop",
            "direction": "above",
        },
        "invalidation": {
            "reference": "m15-invalidation",
            "price_field": "low",
            "price": m15[3]["low"],
            "source": {
                "timeframe": "M15",
                "bar_index": 3,
                "open_time_ms": m15[3]["open_time_ms"],
                "close_time_ms": m15[3]["close_time_ms"],
            },
        },
        "evidence_for": ["breakout"],
        "evidence_against": ["wick"],
        "qualitative_confidence": "medium",
        "uncertainty": ["overlap"],
        "conditions_that_change_market_read": ["invalidation break"],
    }


def test_supervisor_starts_independent_tasks(tmp_path):
    async def exercise():
        sup = BrooksSupervisor(tmp_path, BrooksConfig())
        await sup.start()
        try:
            assert sup.is_running
            names = {task.get_name() for task in sup._children}
            assert names == {
                "brooks-market-clock",
                "brooks-trader",
                "brooks-d1-context",
                "brooks-h4-context",
                "brooks-context-bootstrap",
                "brooks-pm-timer",
                "brooks-watcher",
                "brooks-pm",
                "brooks-gm",
            }
        finally:
            await sup.stop()
        assert not sup.is_running
        assert not sup._children

    asyncio.run(exercise())


def test_clock_wake_math_has_no_drift():
    assert timeframe_ms("H1") == H1
    assert timeframe_ms("1d") == D1
    now = 1_720_000_000_000
    first = latest_due_close(now, H1, 2_000)
    assert first + 2_000 <= now < first + H1 + 2_000
    assert latest_due_close(first + 2_000, H1, 2_000) == first
    assert latest_due_close(first + H1 + 2_000, H1, 2_000) == first + H1
    with pytest.raises(ValueError):
        latest_due_close(-1, H1, 2_000)


def test_clock_rejects_forming_and_future_bars(tmp_path):
    decision = h1_decision()
    published = []
    good = closed_bars(H1, decision, 4)

    async def exercise():
        forming = [dict(bar) for bar in good]
        forming[-1] = dict(forming[-1])
        del forming[-1]["closed"]
        clock = MarketClock(
            symbols=[SYMBOL],
            source=FakeSource({"1h": forming}),
            publish=published.append,
        )
        with pytest.raises(ClosedBarError):
            await clock.publish_closed_bar(SYMBOL, "1h", decision, EventType.H1_BAR_CLOSED)

        future = closed_bars(H1, decision + 2 * H1, 4)
        clock.source = FakeSource({"1h": future})
        with pytest.raises(ClosedBarError):
            await clock.publish_closed_bar(SYMBOL, "1h", decision, EventType.H1_BAR_CLOSED)
        assert published == []

        clock.source = FakeSource({"1h": good})
        event = await clock.publish_closed_bar(SYMBOL, "1h", decision, EventType.H1_BAR_CLOSED)
        assert event.type == EventType.H1_BAR_CLOSED
        assert event.payload["decision_time_ms"] == decision
        assert event.correlation_id == f"{SYMBOL}-1h-{decision}"
        assert published == [event]

    asyncio.run(exercise())


def test_clock_run_publishes_without_drift(tmp_path):
    decision = h1_decision()
    start_ms = decision + 2_000 - 30_000
    now = [start_ms]
    seen = []

    async def fake_sleep(delay_sec):
        now[0] += 10_000
        await asyncio.sleep(0)

    class DynamicSource:
        async def fetch_candles(self, symbol, timeframe, limit):
            interval = {"1h": H1, "1d": D1}[timeframe]
            end = (now[0] // interval) * interval - 1
            return closed_bars(interval, end, max(limit, 2))

    async def exercise():
        clock = MarketClock(
            symbols=[SYMBOL],
            source=DynamicSource(),
            publish=seen.append,
            now_fn=lambda: now[0],
            sleep_fn=fake_sleep,
        )
        stop = asyncio.Event()
        resumed = asyncio.Event()
        resumed.set()
        task = asyncio.create_task(clock.run(stop, resumed.wait))
        try:
            async def _wait_two():
                while len([e for e in seen if e.type == EventType.H1_BAR_CLOSED]) < 2:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_wait_two(), timeout=15)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)
        closes = [e.payload["close_time_ms"] for e in seen if e.type == EventType.H1_BAR_CLOSED][:2]
        assert closes[1] - closes[0] == H1
        assert closes[0] == decision - H1

    asyncio.run(exercise())


def test_clock_hourly_cadence_survives_the_daily_schedule(tmp_path):
    decision = h1_decision()
    start_ms = decision + 2_000 - 30_000
    now = [start_ms]
    seen = []
    sleeps = []
    offsets = {"1h": 2_000, "1d": 3_000}

    async def fake_sleep(delay_sec):
        sleeps.append(delay_sec)
        now[0] += max(int(delay_sec * 1000), 1)
        await asyncio.sleep(0)

    class DynamicSource:
        async def fetch_candles(self, symbol, timeframe, limit):
            interval = {"1h": H1, "1d": D1}[timeframe]
            due = latest_due_close(now[0], interval, offsets[timeframe])
            return closed_bars(interval, due, max(limit, 2))

    async def exercise():
        clock = MarketClock(
            symbols=[SYMBOL],
            source=DynamicSource(),
            publish=seen.append,
            now_fn=lambda: now[0],
            sleep_fn=fake_sleep,
        )
        stop = asyncio.Event()
        resumed = asyncio.Event()
        resumed.set()
        task = asyncio.create_task(clock.run(stop, resumed.wait))
        try:

            async def _wait_four():
                while (
                    len([e for e in seen if e.type == EventType.H1_BAR_CLOSED]) < 4
                ):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_wait_four(), timeout=15)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)
        closes = [
            e.payload["close_time_ms"]
            for e in seen
            if e.type == EventType.H1_BAR_CLOSED
        ][:4]
        assert all(after - before == H1 for before, after in zip(closes, closes[1:]))
        assert any(e.type == EventType.D1_BAR_CLOSED for e in seen)
        # The daily sibling must never put the hourly schedule to sleep past
        # its next close (regression: it kept only the daily delay).
        assert max(sleeps) <= (H1 + 3_000) / 1000

    asyncio.run(exercise())


def test_trader_and_htf_publish_through_supervisor(tmp_path, monkeypatch):
    from condor.brooks.contracts import MarketContextV2, TradeIntentV2

    decision = h1_decision()
    m15 = closed_bars(900_000, decision, 120)
    bars = {
        "4h": closed_bars(14_400_000, decision, 120),
        "1h": closed_bars(H1, decision, 120),
        "15m": m15,
        "1d": closed_bars(D1, decision, 120),
    }
    intent = TradeIntentV2.model_validate(trader_intent_decision(decision, m15))

    async def fake_trader_run(role, prompt, output_model, market_tools, **kwargs):
        assert role == "TRADER"
        return intent

    async def fake_context_run(role, prompt, output_model, market_tools, **kwargs):
        assert role == "CONTEXT_ANALYST"
        label = "D1" if prompt["timeframe"] == "1d" else "H4"
        return MarketContextV2.model_validate(
            {
                "schema": "brooks.market-context.v2",
                "role": "CONTEXT_ANALYST",
                "symbol": SYMBOL,
                "decision_time_ms": decision,
                "timeframe": label,
                "window_bars": 120,
                "primary_regime": "bull-trend",
                "phase": "channel",
                "breakout_mode": False,
                "directional_pressure": "bull",
                "always_in": "long",
                "always_in_relevance": "medium",
                "observations": ["trend"],
                "structures": [],
                "evidence_for": ["higher closes"],
                "evidence_against": ["range"],
                "transition_conditions": ["two-sided overlap"],
                "missing_information": [],
                "confidence": "medium",
            }
        )

    monkeypatch.setattr("condor.brooks.trader.run_role", fake_trader_run)
    async def fake_role(role, prompt, output_model, market_tools, **kwargs):
        if role == "TRADER":
            return await fake_trader_run(role, prompt, output_model, market_tools, **kwargs)
        return await fake_context_run(role, prompt, output_model, market_tools, **kwargs)

    monkeypatch.setattr("condor.brooks.agent_runner.run_role", fake_role)

    async def exercise():
        sup = BrooksSupervisor(
            tmp_path, BrooksConfig(),
            candle_source=FakeSource(bars), agent_key="test-key",
        )
        await sup.start()
        try:
            await asyncio.sleep(0.3)
            inbox = sup.events.subscribe()
            await sup.events.publish(
                BrooksEvent(type=EventType.H4_BAR_CLOSED, symbol=SYMBOL,
                            payload={"decision_time_ms": decision}, correlation_id="run-h4")
            )
            h4_event = await collect_until(
                inbox, lambda e: e.type == EventType.MARKET_CONTEXT_UPDATED
                and e.payload.get("timeframe") == "H4")
            await sup.events.publish(
                BrooksEvent(type=EventType.D1_BAR_CLOSED, symbol=SYMBOL,
                            payload={"decision_time_ms": decision}, correlation_id="run-d1")
            )
            context_event = await collect_until(
                inbox, lambda e: e.type == EventType.MARKET_CONTEXT_UPDATED
                and e.payload.get("timeframe") == "D1")
            await sup.events.publish(
                BrooksEvent(type=EventType.H1_BAR_CLOSED, symbol=SYMBOL,
                            payload={"decision_time_ms": decision}, correlation_id="run-h1")
            )
            intent_event = await collect_until(
                inbox, lambda e: e.type == EventType.TRADER_INTENT_CREATED)
            assert context_event.payload["market_context"]["decision_time_ms"] == decision
            assert h4_event.payload["market_context"]["decision_time_ms"] == decision
            assert intent_event.payload["intent"]["decision"] == "ENTER_LONG"
            assert sup.store.read_market_context("D1")["decision_time_ms"] == decision
            assert sup.store.read_market_context("H4")["decision_time_ms"] == decision
            assert sup.store.read_latest("trader")["decision"] == "ENTER_LONG"
            assert any(e.type == EventType.D1_BAR_CLOSED for e in sup.store.read_events())
        finally:
            await sup.stop()

    asyncio.run(exercise())


class _NoopSource:
    async def fetch_candles(self, symbol, timeframe, limit):
        return []


def test_shadow_mode_switch_defaults_to_shadow_and_wires_the_trader(tmp_path):
    assert BrooksConfig().shadow_mode is True
    assert (
        BrooksConfig.from_engine_config({"brooks": {"shadow_mode": False}}).shadow_mode
        is False
    )

    async def exercise():
        live = BrooksSupervisor(
            tmp_path / "live",
            BrooksConfig(),
            candle_source=_NoopSource(),
            agent_key="test-key",
        )
        live.attach_shadow_mode(False)
        await live.start()
        try:
            assert live._trader is not None
            assert live._trader.shadow_mode is False
        finally:
            await live.stop()

        shadow = BrooksSupervisor(
            tmp_path / "shadow",
            BrooksConfig(),
            candle_source=_NoopSource(),
            agent_key="test-key",
        )
        await shadow.start()
        try:
            assert shadow._trader is not None
            assert shadow._trader.shadow_mode is True
        finally:
            await shadow.stop()

    asyncio.run(exercise())


def test_wire_supervisor_applies_configured_shadow_mode(tmp_path):
    from types import SimpleNamespace

    from condor.brooks.adapters import wire_supervisor

    async def exercise(shadow_mode):
        strategy_home = tmp_path / f"wired-{shadow_mode}"
        supervisor = BrooksSupervisor(strategy_home, BrooksConfig())
        engine_config = {
            "brooks": {
                "symbols": [SYMBOL],
                "account_name": "acct",
                "connector_name": "binance_perpetual_demo",
                "controller_id": "ctrl",
                "shadow_mode": shadow_mode,
            }
        }

        async def get_client():
            return SimpleNamespace()

        result = await wire_supervisor(
            supervisor,
            engine_config,
            strategy_home=strategy_home,
            agent_key="test-key",
            user_id=None,
            agent_id="ctrl",
            get_client=get_client,
        )
        assert result.ok
        return supervisor

    assert asyncio.run(exercise(False))._shadow_mode is False
    assert asyncio.run(exercise(True))._shadow_mode is True


def test_pm_wake_gating_holds_without_active_position(tmp_path):
    decision = 1_000
    holder = {"active": False}
    runner_calls = []

    def load_context(correlation_id):
        assert correlation_id == "t1"
        if holder["active"]:
            return {
                "correlation_id": "t1",
                "symbol": SYMBOL,
                "decision_time_ms": decision,
                "position_active": True,
                "positions": [{"position_id": "p1", "quantity": "1"}],
            }
        return {
            "correlation_id": "t1",
            "symbol": SYMBOL,
            "decision_time_ms": decision,
            "position_active": False,
            "positions": [{"position_id": "p1", "quantity": "0"}],
        }

    class FakeRunner:
        async def run(self, role, **kwargs):
            runner_calls.append(role)
            return {
                "schema": "brooks.management-decision.v2",
                "role": "POSITION_MANAGER",
                "decision_time_ms": decision,
                "action": "HOLD",
                "position_ids": ["p1"],
                "reason": "steady",
                "evidence": {"observations": ["o"], "evidence_for": ["f"], "evidence_against": ["a"]},
                "risk": {"exposure_before": ["b"], "exposure_after": ["a"],
                         "protection_status": "adequate", "costs_considered": ["c"],
                         "uncertainty": "low"},
                "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
                "hedge_plan": None,
                "market_analysis_request": None,
                "conditions_that_change_action": ["c"],
            }

    async def exercise():
        sup = BrooksSupervisor(tmp_path, BrooksConfig(), agent_key="test-key")
        sup.attach_pm(runner=FakeRunner(), load_context=load_context,
                      record_market_read=lambda cid, record: None)
        await sup.start()
        try:
            await asyncio.sleep(0.3)
            inbox = sup.events.subscribe({EventType.MANAGEMENT_INTENT_CREATED})
            await sup.events.publish(
                BrooksEvent(type=EventType.TRADER_INTENT_CREATED, symbol=SYMBOL,
                            payload={}, correlation_id="t1")
            )
            await asyncio.sleep(0.5)
            assert runner_calls == []
            assert inbox.empty()

            holder["active"] = True
            await sup.events.publish(
                BrooksEvent(type=EventType.TRADER_INTENT_CREATED, symbol=SYMBOL,
                            payload={}, correlation_id="t1")
            )
            created = await wait_for(inbox)
            assert created.type == EventType.MANAGEMENT_INTENT_CREATED
            assert runner_calls == ["POSITION_MANAGER"]
            saved = sup.store.read_trade_document("t1", "latest_management_intent.json")
            assert saved["action"] == "HOLD"
        finally:
            await sup.stop()

    asyncio.run(exercise())


def watcher_snapshot(main_qty, fills_cursor="f0", fills=()):
    return {
        "correlation_id": "t1",
        "symbol": SYMBOL,
        "main": {"position_id": "m1", "side": "LONG", "qty": str(main_qty)},
        "hedge": {"position_id": "", "side": "", "qty": "0"},
        "executors": [],
        "open_orders": [],
        "recent_fills": list(fills),
        "fills_cursor": fills_cursor,
    }


def test_watcher_publishes_transitions():
    current = {"snapshots": [watcher_snapshot(0)]}
    emitted = []

    async def exercise():
        watcher = PositionWatcher(
            lambda: list(current["snapshots"]), emitted.append)
        assert await watcher.poll() == []
        current["snapshots"] = [watcher_snapshot(1)]
        first = await watcher.poll()
        assert [e["type"] for e in first] == ["POSITION_OPENED"]
        assert await watcher.poll() == []
        current["snapshots"] = [watcher_snapshot(2)]
        assert [e["type"] for e in await watcher.poll()] == ["POSITION_CHANGED"]
        current["snapshots"] = [watcher_snapshot(2, fills_cursor="f1", fills=[{"fill_id": "x"}])]
        assert [e["type"] for e in await watcher.poll()] == ["FILL"]
        current["snapshots"] = [watcher_snapshot(0, fills_cursor="f1", fills=[{"fill_id": "x"}])]
        assert [e["type"] for e in await watcher.poll()] == ["POSITION_CLOSED"]

    asyncio.run(exercise())


def test_watcher_loop_publishes_on_supervisor_start(tmp_path):
    async def exercise():
        sup = BrooksSupervisor(tmp_path, BrooksConfig())
        sup.attach_watcher_snapshots(lambda: [watcher_snapshot(1)])
        await sup.start()
        try:
            inbox = sup.events.subscribe({EventType.POSITION_OPENED})
            opened = await wait_for(inbox)
            assert opened.correlation_id == "t1"
        finally:
            await sup.stop()

    asyncio.run(exercise())


class FakeGM:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.entries = []
        self.managements = []

    async def execute_entry(self, intent, *, correlation_id):
        self.entries.append((intent, correlation_id))
        if self.mode == "reject":
            raise GMRejected("risk cap")
        if self.mode == "boom":
            raise RuntimeError("timeout, unknown acceptance")
        return {"status": "submitted", "correlation_id": correlation_id}

    async def execute_management(self, *, correlation_id, decision_id, action, reduce_fraction=None):
        self.managements.append((correlation_id, decision_id, action))
        if action not in ("HOLD", "REDUCE", "CLOSE"):
            raise GMRejected("unsupported management action")
        return {"action": action, "status": "no_write" if action == "HOLD" else "submitted"}


def gm_event(event_type, payload, correlation_id="t1"):
    return BrooksEvent(type=event_type, symbol=SYMBOL, payload=payload,
                       correlation_id=correlation_id)


def test_gm_consumer_entry_and_management_routing():
    async def exercise():
        emitted = []
        gm = FakeGM()
        consumer = GMConsumer(gm_factory=lambda symbol: gm, publish=emitted.append)

        approved = await consumer.handle(
            gm_event(EventType.TRADER_INTENT_CREATED,
                     {"intent": {"decision": "ENTER_LONG", "symbol": SYMBOL}}))
        assert approved["type"] == EventType.GM_ENTRY_APPROVED.value
        assert gm.entries[0][1] == "t1"

        assert await consumer.handle(
            gm_event(EventType.TRADER_INTENT_CREATED, {"intent": {"decision": "NO_TRADE"}})) is None
        assert len(gm.entries) == 1

        held = await consumer.handle(
            gm_event(EventType.MANAGEMENT_INTENT_CREATED, {"action": "HOLD"}))
        assert held["type"] == EventType.GM_MANAGEMENT_APPROVED.value

        rejected = await consumer.handle(
            gm_event(EventType.MANAGEMENT_INTENT_CREATED, {"action": "REMOVE_HEDGE"}))
        assert rejected["type"] == EventType.GM_MANAGEMENT_REJECTED.value

        attention = await consumer.handle(
            gm_event(EventType.MANAGEMENT_INTENT_CREATED, {"action": "REQUEST_MARKET_ANALYSIS"}))
        assert attention["type"] == EventType.RECONCILIATION_REQUIRED.value

        assert await consumer.handle(
            gm_event(EventType.MANAGEMENT_INTENT_CREATED, {"action": "MANAGEMENT_BLOCKED"})) is None

        denying = GMConsumer(gm_factory=lambda symbol: FakeGM(mode="reject"),
                             publish=emitted.append)
        entry_rejected = await denying.handle(
            gm_event(EventType.TRADER_INTENT_CREATED,
                     {"intent": {"decision": "ENTER_SHORT", "symbol": SYMBOL}}))
        assert entry_rejected["type"] == EventType.GM_ENTRY_REJECTED.value
        assert entry_rejected["payload"]["reason"] == "risk cap"

        ambiguous = GMConsumer(gm_factory=lambda symbol: FakeGM(mode="boom"),
                               publish=emitted.append)
        reconciling = await ambiguous.handle(
            gm_event(EventType.TRADER_INTENT_CREATED,
                     {"intent": {"decision": "ENTER_SHORT", "symbol": SYMBOL}}))
        assert reconciling["type"] == EventType.RECONCILIATION_REQUIRED.value

        unconfigured = GMConsumer(gm_factory=None, publish=emitted.append)
        missing = await unconfigured.handle(
            gm_event(EventType.TRADER_INTENT_CREATED,
                     {"intent": {"decision": "ENTER_LONG", "symbol": SYMBOL}}))
        assert missing["type"] == EventType.RECONCILIATION_REQUIRED.value

    asyncio.run(exercise())


def test_pause_resume_holds_children_at_safe_points(tmp_path):
    handled = []

    async def exercise():
        sup = BrooksSupervisor(tmp_path, BrooksConfig())
        sup.attach_trader_handler(handled.append)
        await sup.start()
        try:
            await asyncio.sleep(0.3)
            sup.pause()
            await sup.events.publish(
                BrooksEvent(type=EventType.H1_BAR_CLOSED, symbol=SYMBOL, payload={}))
            await asyncio.sleep(0.4)
            assert handled == []
            sup.resume()
            deadline = asyncio.get_running_loop().time() + 5
            while not handled and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert len(handled) == 1
        finally:
            await sup.stop()

    asyncio.run(exercise())


def test_stop_cancels_children_and_flushes(tmp_path):
    async def exercise():
        sup = BrooksSupervisor(tmp_path, BrooksConfig())
        sup.attach_trader_handler(lambda event: asyncio.sleep(0))
        await sup.start()
        await asyncio.sleep(0.2)
        await sup.events.publish(
            BrooksEvent(type=EventType.H1_BAR_CLOSED, symbol=SYMBOL, payload={}))
        await asyncio.sleep(0.3)
        await sup.stop()
        assert not sup.is_running
        assert not sup._children
        assert sup.store.read_events()
        with pytest.raises(RuntimeError):
            await sup.events.publish(
                BrooksEvent(type=EventType.PM_TIMER, symbol="*", payload={}))

    asyncio.run(exercise())
