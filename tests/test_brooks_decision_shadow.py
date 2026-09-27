"""Zero-write guarantee in shadow mode across GMConsumer and BrooksGM."""

import time
from decimal import Decimal

import pytest

from condor.brooks.contracts import TradeIntentV2
from condor.brooks.events import BrooksEvent, EventType
from condor.brooks.gm import (
    AccountSnapshot,
    BrooksGM,
    GMPolicy,
    VenueRules,
)
from condor.brooks.supervisor import GMConsumer


class ExplodingExecutionPort:
    """ExecutionPort whose every method raises AssertionError if called."""

    controller_id = "brooks"

    async def get_position_mode(self):
        raise AssertionError("get_position_mode called on ExplodingExecutionPort")

    async def open_main(self, **kwargs):
        raise AssertionError("open_main called on ExplodingExecutionPort")

    async def reduce_main(self, **kwargs):
        raise AssertionError("reduce_main called on ExplodingExecutionPort")

    async def close_main(self, **kwargs):
        raise AssertionError("close_main called on ExplodingExecutionPort")

    async def execute_hedge(self, **kwargs):
        raise AssertionError("execute_hedge called on ExplodingExecutionPort")

    async def get_state(self, **kwargs):
        raise AssertionError("get_state called on ExplodingExecutionPort")


class FakeReader:
    def __init__(self, state=None):
        self.state = state or AccountSnapshot(
            as_of_ms=int(time.time() * 1000),
            equity=Decimal("1000"),
            available_margin=Decimal("500"),
            mark_price=Decimal("100"),
            gross_exposure=Decimal("0"),
            open_positions=0,
            rules=VenueRules(Decimal("0.01"), Decimal("0.01"), Decimal("10"), 5),
        )

    async def read(self, **kwargs):
        return self.state


def valid_intent(decision="ENTER_LONG") -> dict:
    source = {
        "timeframe": "M15",
        "bar_index": 0,
        "open_time_ms": 0,
        "close_time_ms": 899_999,
    }
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": decision,
        "symbol": "BTC-USDT",
        "decision_time_ms": 899_999,
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
            "kind": "stop",
            "direction": "above",
            "reference": "bar high",
            "price_field": "high",
            "price": "101",
            "source": source,
        },
        "invalidation": {
            "reference": "bar low",
            "price_field": "low",
            "price": "99",
            "source": source,
        },
        "evidence_for": ["breakout"],
        "evidence_against": ["resistance"],
        "qualitative_confidence": "medium",
        "uncertainty": ["continuation"],
        "conditions_that_change_market_read": ["breakdown"],
    }


def make_gm(tmp_path, port=None, reader=None):
    port = port or ExplodingExecutionPort()
    reader = reader or FakeReader()
    policy = GMPolicy(
        risk_per_trade_pct=Decimal("0.01"),
        max_positions=2,
        max_gross_exposure_pct=Decimal("2"),
        leverage=2,
        take_profit_r=Decimal("2"),
        time_limit_sec=3600,
    )
    return BrooksGM(
        state_root=tmp_path,
        account_name="test_acc",
        connector_name="binance_perpetual",
        reader=reader,
        execution=port,
        policy=policy,
    )


@pytest.mark.asyncio
async def test_gm_consumer_and_brooks_gm_shadow_mode_zero_writes(tmp_path):
    """Mandatory test: ExplodingExecutionPort raises AssertionError on any write;

    drive shadow TradeIntent through real GMConsumer + real BrooksGM; assert
    ZERO port calls and no write-amplifying event published.
    """
    exploding_port = ExplodingExecutionPort()
    real_gm = make_gm(tmp_path, port=exploding_port)
    published = []
    consumer = GMConsumer(
        gm_factory=lambda symbol: real_gm,
        publish=published.append,
    )

    # Validate intent schema contract
    trade_intent = TradeIntentV2.model_validate(valid_intent())

    event = BrooksEvent(
        type=EventType.TRADER_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="corr-shadow-1",
        payload={
            "intent": trade_intent.model_dump(),
            "shadow_mode": True,
        },
    )

    # Drive through GMConsumer + real BrooksGM
    result = await consumer.handle(event)

    # Assert no write-amplifying event published
    assert result is None
    assert len(published) == 0

    # Assert zero disk artifacts created
    assert not (tmp_path / "trades" / "corr-shadow-1").exists()


@pytest.mark.asyncio
async def test_brooks_gm_execute_entry_fails_closed_on_shadow(tmp_path):
    """Defense-in-depth: BrooksGM.execute_entry fails closed on shadow intent."""
    exploding_port = ExplodingExecutionPort()
    real_gm = make_gm(tmp_path, port=exploding_port)

    # 1. Via shadow_mode=True parameter
    res1 = await real_gm.execute_entry(
        valid_intent(),
        correlation_id="corr-shadow-param",
        shadow_mode=True,
    )
    assert res1 is None
    assert not (tmp_path / "trades" / "corr-shadow-param").exists()

    # 2. Via intent dict carrying shadow_mode: True
    intent_dict = valid_intent()
    intent_dict["shadow_mode"] = True
    res2 = await real_gm.execute_entry(
        intent_dict,
        correlation_id="corr-shadow-dict",
    )
    assert res2 is None
    assert not (tmp_path / "trades" / "corr-shadow-dict").exists()

    # 3. Via payload dict with {"intent": ..., "shadow_mode": True}
    res3 = await real_gm.execute_entry(
        {"intent": valid_intent(), "shadow_mode": True},
        correlation_id="corr-shadow-payload",
    )
    assert res3 is None
    assert not (tmp_path / "trades" / "corr-shadow-payload").exists()


@pytest.mark.asyncio
async def test_gm_consumer_and_brooks_gm_shadow_management_zero_writes(tmp_path):
    """Defense-in-depth: shadow management intents perform zero writes."""
    exploding_port = ExplodingExecutionPort()
    real_gm = make_gm(tmp_path, port=exploding_port)
    published = []
    consumer = GMConsumer(
        gm_factory=lambda symbol: real_gm,
        publish=published.append,
    )

    mgmt_event = BrooksEvent(
        type=EventType.MANAGEMENT_INTENT_CREATED,
        symbol="BTC-USDT",
        correlation_id="corr-shadow-mgmt",
        payload={
            "action": "CLOSE",
            "decision_id": "d-shadow",
            "shadow_mode": True,
        },
    )
    result = await consumer.handle(mgmt_event)
    assert result is None
    assert len(published) == 0

    # Also test direct execute_management in shadow mode
    res_direct = await real_gm.execute_management(
        correlation_id="corr-shadow-mgmt",
        decision_id="d-shadow-direct",
        action="CLOSE",
        shadow_mode=True,
    )
    assert res_direct["status"] == "no_write"
    assert res_direct["shadow_mode"] is True
