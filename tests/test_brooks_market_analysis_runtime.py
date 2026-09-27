"""Integration tests for Brooks REQUEST_MARKET_ANALYSIS runtime handshake (Finding 10).

Verifies class-level runtime integration:
1) A PM decision with REQUEST_MARKET_ANALYSIS drives sanitize -> analysis runner (fake) ->
   response persisted -> PM wake published with the analysis in the payload - and NO execution port call.
2) A request or analysis payload carrying any private field is rejected fail-closed
   (no analysis run, no PM wake, no execution port call).
3) The anti-loop guard blocks a second back-to-back analysis cycle both at the PM
   and GMConsumer runtime boundaries.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from condor.brooks.contracts import (
    ManagementDecisionV2,
    MarketAnalysisResponseV1,
    TradeIntentV2,
)
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.market_analysis import (
    RESPONSE_SCHEMA,
    MarketAnalysisBoundaryError,
    MarketAnalysisLeakError,
    MarketAnalysisLoopBlockedError,
    MarketAnalysisRequestV1,
    MarketAnalysisTraderBoundaryViolation,
    check_pm_anti_loop_guard,
)
from condor.brooks.pm import PositionManager
from condor.brooks.store import BrooksStore
from condor.brooks.supervisor import BrooksSupervisor, GMConsumer

# ---------------------------------------------------------------------------
# Test Fakes and Fixtures
# ---------------------------------------------------------------------------


def valid_request_dict(
    symbol: str = "BTC-USDT", req_id: str = "req-runtime-01"
) -> dict[str, Any]:
    return {
        "schema": "brooks.market-analysis-request.v1",
        "request_id": req_id,
        "symbol": symbol,
        "decision_time_ms": 1700000000000,
        "timeframes": ["15m", "1h", "4h"],
        "market_fields": ["ordered_ohlc", "bar_by_bar", "decision_time"],
    }


def valid_trade_intent_dict(
    symbol: str = "BTC-USDT", decision: str = "NO_TRADE"
) -> dict[str, Any]:
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": decision,
        "symbol": symbol,
        "decision_time_ms": 1700000000000,
        "market_context": None,
        "setup": {
            "trigger_status": "present",
            "location_assessment": "neutral",
            "no_trade_reason": "weak_signal",
        },
        "decision_timeframe": None,
        "context_timeframes_used": ["1h", "4h"],
        "entry_mechanism": "none",
        "trigger": None,
        "invalidation": None,
        "evidence_for": ["tested key level"],
        "evidence_against": ["lack of volume"],
        "qualitative_confidence": "medium",
        "uncertainty": ["range bound"],
        "conditions_that_change_market_read": ["breakout above range high"],
    }


class RecordingExecutionPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def open_main(self, **kwargs: Any) -> str:
        self.calls.append(("open", kwargs))
        return "exec-main"

    async def reduce_main(self, **kwargs: Any) -> str:
        self.calls.append(("reduce", kwargs))
        return "exec-reduce"

    async def close_main(self, **kwargs: Any) -> str:
        self.calls.append(("close", kwargs))
        return str(kwargs.get("executor_id", "exec-close"))

    async def execute_hedge(self, **kwargs: Any) -> str:
        self.calls.append(("hedge", kwargs))
        return "exec-hedge"


class FakeGM:
    def __init__(self, port: RecordingExecutionPort | None = None) -> None:
        self.port = port or RecordingExecutionPort()
        self.management_calls: list[dict[str, Any]] = []

    async def execute_entry(
        self, intent: Any, correlation_id: str | None = None
    ) -> dict[str, Any]:
        return {"binding_id": "b-test-01"}

    async def execute_management(self, **kwargs: Any) -> dict[str, Any]:
        self.management_calls.append(kwargs)
        action = kwargs.get("action")
        if action == "REDUCE":
            await self.port.reduce_main(symbol="BTC-USDT", quantity=Decimal("0.5"))
        elif action == "CLOSE":
            await self.port.close_main(executor_id="exec-1")
        return {"action": action, "status": "executed"}


class FakeMarketAnalysisRunner:
    def __init__(self, return_intent: Any | None = None) -> None:
        self.calls: list[MarketAnalysisRequestV1] = []
        self.return_intent = (
            return_intent if return_intent is not None else valid_trade_intent_dict()
        )

    async def __call__(self, clean_request: MarketAnalysisRequestV1) -> Any:
        self.calls.append(clean_request)
        if callable(self.return_intent):
            return self.return_intent(clean_request)
        return self.return_intent


def valid_pm_decision_dict(
    action: str = "REQUEST_MARKET_ANALYSIS",
    decision_time_ms: int = 1700000000000,
    req_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision: dict[str, Any] = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": decision_time_ms,
        "action": action,
        "position_ids": ["pos-001"],
        "reason": "testing runtime handshake",
        "evidence": {
            "observations": ["tested resistance"],
            "evidence_for": ["weak bull follow-through"],
            "evidence_against": ["higher timeframe bullish"],
        },
        "risk": {
            "exposure_before": ["1.0 BTC"],
            "exposure_after": ["1.0 BTC"],
            "protection_status": "adequate",
            "costs_considered": ["trading fees"],
            "uncertainty": "medium",
        },
        "execution": {
            "orders": [],
            "cancel_order_ids": [],
            "replace_orders": [],
        },
        "conditions_that_change_action": ["breakout above range high"],
        "hedge_plan": None,
        "market_analysis_request": (
            req_dict if action == "REQUEST_MARKET_ANALYSIS" else None
        ),
        "reduce_fraction": None,
    }
    return decision


# ---------------------------------------------------------------------------
# Test 1: Full Handshake Flow & Zero-Write Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pm_decision_drives_runtime_handshake_and_no_execution_write(
    tmp_path: Path,
) -> None:
    """1) A PM decision with REQUEST_MARKET_ANALYSIS drives sanitize -> analysis runner (fake) ->

    response persisted -> PM wake published with the analysis in the payload - and NO execution port call.
    """
    store = BrooksStore(tmp_path)
    events = EventBus(store)
    port = RecordingExecutionPort()
    gm_mock = FakeGM(port=port)
    fake_runner = FakeMarketAnalysisRunner()

    published_events: list[BrooksEvent] = []
    subscriber_queue = events.subscribe()

    consumer = GMConsumer(
        gm_factory=lambda s: gm_mock,
        publish=events,
        market_analysis_runner=fake_runner,
        store=store,
    )

    req = valid_request_dict(symbol="BTC-USDT", req_id="req-handshake-1")
    pm_decision_payload = valid_pm_decision_dict(
        action="REQUEST_MARKET_ANALYSIS", req_dict=req
    )

    # Validate contracts wire compatibility
    decision_model = ManagementDecisionV2.model_validate(pm_decision_payload)
    assert decision_model.action == "REQUEST_MARKET_ANALYSIS"

    pm_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c-handshake-1",
        causation_id="cause-pm-01",
        payload=decision_model.model_dump(mode="json"),
    )

    # Consumer processes the PM decision
    result = await consumer.handle(pm_event)

    # 1. Verify analyst runner was called with sanitized request (zero private leaks)
    assert len(fake_runner.calls) == 1
    clean_req = fake_runner.calls[0]
    assert isinstance(clean_req, MarketAnalysisRequestV1)
    assert clean_req.symbol == "BTC-USDT"
    assert clean_req.request_id == "req-handshake-1"
    assert clean_req.decision_time_ms == 1700000000000

    # 2. Verify NO execution port or GM management write calls occurred
    assert len(port.calls) == 0, f"Expected zero execution port calls, got {port.calls}"
    assert (
        len(gm_mock.management_calls) == 0
    ), "GM execute_management must NEVER be called on REQUEST_MARKET_ANALYSIS"

    # 3. Verify emitted event is MARKET_ANALYSIS_COMPLETED with analysis in payload
    assert result is not None
    assert result.type == EventType.MARKET_ANALYSIS_COMPLETED
    assert result.symbol == "BTC-USDT"
    assert result.correlation_id == "c-handshake-1"
    assert result.causation_id == pm_event.event_id

    analysis_payload = result.payload.get("market_analysis")
    assert analysis_payload is not None
    assert analysis_payload["schema"] == RESPONSE_SCHEMA
    assert analysis_payload["symbol"] == "BTC-USDT"
    assert analysis_payload["request_id"] == "req-handshake-1"
    assert analysis_payload["trade_intent"]["role"] == "TRADER"
    assert analysis_payload["trade_intent"]["decision"] == "NO_TRADE"

    # 4. Verify durable persistence via store and bus seams
    store_events = store.read_events()
    assert any(
        e.type == EventType.MARKET_ANALYSIS_COMPLETED
        and e.payload.get("market_analysis") is not None
        for e in store_events
    )

    # 5. Verify PM wakes again on MARKET_ANALYSIS_COMPLETED with analysis available in context
    pm_calls: list[dict[str, Any]] = []

    async def fake_pm_runner(
        role: str, prompt: dict[str, Any], **kwargs: Any
    ) -> ManagementDecisionV2:
        pm_calls.append(prompt)
        # PM now has the market analysis available and decides HOLD
        hold_decision = valid_pm_decision_dict(action="HOLD", req_dict=None)
        return ManagementDecisionV2.model_validate(hold_decision)

    pm_published: list[BrooksEvent] = []
    pm = PositionManager(
        runner=fake_pm_runner,
        load_context=lambda c_id: {
            "correlation_id": c_id,
            "symbol": "BTC-USDT",
            "decision_time_ms": 1700000000000,
            "position": {"position_id": "pos-001", "quantity": "1.0"},
            "market_analysis": None,  # Context slot initially None
        },
        save_decision=lambda c_id, d: None,
        publish=pm_published.append,
        candle_source=None,
        record_market_read=lambda c_id, r: None,
    )

    # Send the wake event to PositionManager
    woken_decision = await pm.handle_event(result)

    assert woken_decision is not None
    assert woken_decision.action == "HOLD"
    assert len(pm_calls) == 1
    # Verify the PM prompt received the market_analysis slot from the wake event
    assert "market_analysis" in pm_calls[0]
    assert pm_calls[0]["market_analysis"] is not None
    assert pm_calls[0]["market_analysis"]["schema"] == RESPONSE_SCHEMA
    assert pm_calls[0]["market_analysis"]["request_id"] == "req-handshake-1"

    # Verify PM published MANAGEMENT_INTENT_CREATED with action HOLD
    assert len(pm_published) == 1
    action_published = (
        pm_published[0].payload["action"]
        if hasattr(pm_published[0], "payload")
        else pm_published[0]["payload"]["action"]
    )
    assert action_published == "HOLD"

    events.close()


# ---------------------------------------------------------------------------
# Test 2: Private Leak Fail-Closed Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaking_key,leaking_val",
    [
        ("unrealized_pnl", "+150.00"),
        ("realized_pnl", "+50.00"),
        ("balance", "50000.00"),
        ("equity", "51000.00"),
        ("available_margin", "25000.00"),
        ("position_size", "1.5"),
        ("quantity", "1.5"),
        ("orders", [{"order_id": "ord-secret"}]),
        ("fills", [{"fill_id": "fill-01"}]),
        ("account", "account-priv-99"),
        ("entry_price", "62000.00"),
        ("hedge_ratio", "0.4"),
    ],
)
async def test_request_carrying_private_field_rejected_fail_closed(
    tmp_path: Path,
    leaking_key: str,
    leaking_val: Any,
) -> None:
    """2a) A request carrying any private field is rejected fail-closed (no analysis run, no PM wake)."""
    store = BrooksStore(tmp_path)
    events = EventBus(store)
    port = RecordingExecutionPort()
    gm_mock = FakeGM(port=port)
    fake_runner = FakeMarketAnalysisRunner()

    consumer = GMConsumer(
        gm_factory=lambda s: gm_mock,
        publish=events,
        market_analysis_runner=fake_runner,
        store=store,
        raise_boundary_errors=False,
    )

    req = valid_request_dict()
    req[leaking_key] = leaking_val  # Inject private field

    pm_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c-leak-1",
        payload={
            "action": "REQUEST_MARKET_ANALYSIS",
            "market_analysis_request": req,
        },
    )

    result = await consumer.handle(pm_event)

    # 1. No analysis run
    assert (
        len(fake_runner.calls) == 0
    ), f"Analysis runner must NOT be called on private leak {leaking_key}"

    # 2. No PM wake published
    assert result is not None
    assert result.type == EventType.RECONCILIATION_REQUIRED
    assert result.type != EventType.MARKET_ANALYSIS_COMPLETED
    assert "market analysis boundary violation" in result.payload["reason"]
    assert "Private state leak detected" in result.payload["reason"]

    # 3. No execution port call
    assert len(port.calls) == 0
    assert len(gm_mock.management_calls) == 0

    events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaking_key,leaking_val",
    [
        ("position_id", "pos-secret-123"),
        ("unrealized_pnl", "+200.00"),
        ("balance", "10000.00"),
        ("equity", "10200.00"),
        ("position_size", "2.0"),
        ("orders", ["o1"]),
    ],
)
async def test_analyst_payload_carrying_private_field_rejected_fail_closed(
    tmp_path: Path,
    leaking_key: str,
    leaking_val: Any,
) -> None:
    """2b) An analysis payload carrying any private field is rejected fail-closed (no PM wake)."""
    store = BrooksStore(tmp_path)
    events = EventBus(store)
    port = RecordingExecutionPort()
    gm_mock = FakeGM(port=port)

    # Analyst returns trade intent containing private field leak
    leaking_intent = valid_trade_intent_dict()
    leaking_intent[leaking_key] = leaking_val
    fake_runner = FakeMarketAnalysisRunner(return_intent=leaking_intent)

    consumer = GMConsumer(
        gm_factory=lambda s: gm_mock,
        publish=events,
        market_analysis_runner=fake_runner,
        store=store,
        raise_boundary_errors=False,
    )

    pm_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c-leak-trader",
        payload={
            "action": "REQUEST_MARKET_ANALYSIS",
            "market_analysis_request": valid_request_dict(),
        },
    )

    result = await consumer.handle(pm_event)

    # 1. Analyst ran, but output was intercepted and rejected
    assert len(fake_runner.calls) == 1

    # 2. No PM wake published
    assert result is not None
    assert result.type == EventType.RECONCILIATION_REQUIRED
    assert result.type != EventType.MARKET_ANALYSIS_COMPLETED
    assert "boundary violation" in result.payload["reason"].lower()

    # 3. No execution port call
    assert len(port.calls) == 0
    assert len(gm_mock.management_calls) == 0

    events.close()


@pytest.mark.asyncio
async def test_raise_boundary_errors_mode_propagates_leak_exception(
    tmp_path: Path,
) -> None:
    """Verify that when raise_boundary_errors is enabled, the exact MarketAnalysisLeakError raises."""
    events = EventBus(BrooksStore(tmp_path))
    consumer = GMConsumer(
        gm_factory=lambda s: FakeGM(),
        publish=events,
        market_analysis_runner=FakeMarketAnalysisRunner(),
        raise_boundary_errors=True,
    )

    leaking_req = valid_request_dict()
    leaking_req["equity"] = "100000"

    pm_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="c-leak-raise",
        payload={
            "action": "REQUEST_MARKET_ANALYSIS",
            "market_analysis_request": leaking_req,
        },
    )

    with pytest.raises(MarketAnalysisLeakError) as exc_info:
        await consumer.handle(pm_event)

    assert "Private state leak detected" in str(exc_info.value)
    events.close()


# ---------------------------------------------------------------------------
# Test 3: Anti-Loop Guard Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anti_loop_guard_blocks_second_back_to_back_cycle_in_pm(
    tmp_path: Path,
) -> None:
    """3a) The anti-loop guard blocks a second back-to-back analysis cycle at the PM boundary."""
    prior_response = MarketAnalysisResponseV1(
        schema=RESPONSE_SCHEMA,
        request_id="req-prev-01",
        symbol="BTC-USDT",
        decision_time_ms=1700000000000,
        trade_intent=valid_trade_intent_dict(),
    )

    # 1. Direct unit verification of anti-loop guard helper
    with pytest.raises(MarketAnalysisLoopBlockedError):
        check_pm_anti_loop_guard(
            "REQUEST_MARKET_ANALYSIS", prior_market_analysis=prior_response
        )

    # Other actions with prior market analysis are allowed
    check_pm_anti_loop_guard("HOLD", prior_market_analysis=prior_response)
    check_pm_anti_loop_guard("REDUCE", prior_market_analysis=prior_response)
    check_pm_anti_loop_guard("CLOSE", prior_market_analysis=prior_response)

    # Initial cycle without prior market analysis is allowed
    check_pm_anti_loop_guard("REQUEST_MARKET_ANALYSIS", prior_market_analysis=None)

    # 2. PositionManager runtime boundary verification:
    # When PM wakes with market_analysis already in its context, attempting another
    # REQUEST_MARKET_ANALYSIS triggers MarketAnalysisLoopBlockedError fail-closed.
    async def rogue_looping_pm(
        role: str, prompt: dict[str, Any], **kwargs: Any
    ) -> ManagementDecisionV2:
        return ManagementDecisionV2.model_validate(
            valid_pm_decision_dict(
                action="REQUEST_MARKET_ANALYSIS",
                req_dict=valid_request_dict(req_id="req-loop-02"),
            )
        )

    pm = PositionManager(
        runner=rogue_looping_pm,
        load_context=lambda c_id: {
            "correlation_id": c_id,
            "symbol": "BTC-USDT",
            "decision_time_ms": 1700000000000,
            "position": {"position_id": "pos-001", "quantity": "1.0"},
            "market_analysis": prior_response.model_dump(
                mode="json"
            ),  # Already has analysis
        },
        save_decision=lambda c_id, d: None,
        publish=lambda e: None,
        candle_source=None,
        record_market_read=lambda c_id, r: None,
    )

    wake_event = BrooksEvent(
        type=EventType.MARKET_ANALYSIS_COMPLETED,
        symbol="BTC-USDT",
        correlation_id="c-loop-test",
        payload={"market_analysis": prior_response.model_dump(mode="json")},
    )

    with pytest.raises(MarketAnalysisLoopBlockedError):
        await pm.handle_event(wake_event)


@pytest.mark.asyncio
async def test_anti_loop_guard_blocks_second_back_to_back_cycle_in_gm_consumer(
    tmp_path: Path,
) -> None:
    """3b) The anti-loop guard blocks back-to-back analysis calls for the same correlation_id in GMConsumer."""
    store = BrooksStore(tmp_path)
    events = EventBus(store)
    port = RecordingExecutionPort()
    gm_mock = FakeGM(port=port)
    fake_runner = FakeMarketAnalysisRunner()

    consumer = GMConsumer(
        gm_factory=lambda s: gm_mock,
        publish=events,
        market_analysis_runner=fake_runner,
        store=store,
    )

    correlation_id = "c-anti-loop-gm"
    event_1 = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id=correlation_id,
        payload={
            "action": "REQUEST_MARKET_ANALYSIS",
            "market_analysis_request": valid_request_dict(req_id="req-loop-1"),
        },
    )

    # First analysis cycle succeeds
    res1 = await consumer.handle(event_1)
    assert res1 is not None
    assert res1.type == EventType.MARKET_ANALYSIS_COMPLETED
    assert len(fake_runner.calls) == 1

    # Immediate second REQUEST_MARKET_ANALYSIS for the same correlation_id without intervening action
    event_2 = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id=correlation_id,
        payload={
            "action": "REQUEST_MARKET_ANALYSIS",
            "market_analysis_request": valid_request_dict(req_id="req-loop-2"),
        },
    )

    res2 = await consumer.handle(event_2)

    # 1. Anti-loop guard blocked second cycle
    assert res2 is not None
    assert res2.type == EventType.RECONCILIATION_REQUIRED
    assert "anti-loop guard blocked" in res2.payload["reason"]

    # 2. Runner was NOT called a second time
    assert len(fake_runner.calls) == 1

    # 3. No execution port calls
    assert len(port.calls) == 0

    # 4. Now perform an intervening action (e.g. HOLD)
    hold_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id=correlation_id,
        payload={"action": "HOLD"},
    )
    res_hold = await consumer.handle(hold_event)
    res_type = getattr(res_hold, "type", None) or (
        res_hold.get("type") if isinstance(res_hold, dict) else None
    )
    assert res_type in (
        EventType.GM_MANAGEMENT_APPROVED,
        EventType.GM_MANAGEMENT_APPROVED.value,
    )

    # 5. After intervening action, future analysis cycles are unblocked
    event_3 = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id=correlation_id,
        payload={
            "action": "REQUEST_MARKET_ANALYSIS",
            "market_analysis_request": valid_request_dict(req_id="req-loop-3"),
        },
    )
    res3 = await consumer.handle(event_3)
    assert res3 is not None
    assert res3.type == EventType.MARKET_ANALYSIS_COMPLETED
    assert len(fake_runner.calls) == 2

    events.close()
