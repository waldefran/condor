"""Contract and adversarial fixtures for the isolated Brooks agent boundary."""

import pytest
from pydantic import ValidationError

from condor.brooks.contracts import (
    HedgePlanV2,
    ManagementDecisionV2,
    MarketAnalysisRequestV1,
    PositionManagementInputV2,
    TradeIntentV2,
)


def intent() -> dict:
    source = {"timeframe": "H1", "bar_index": 0, "open_time_ms": 0, "close_time_ms": 3_599_999}
    return {
        "schema": "brooks.trade-intent.v2", "role": "TRADER", "decision": "ENTER_LONG",
        "symbol": "BTC-USDT", "decision_time_ms": 3_599_999,
        "market_context": {"trend": "up"}, "setup": {"trigger_status": "present"},
        "decision_timeframe": "H1", "context_timeframes_used": ["H1"],
        "entry_mechanism": "breakout",
        "trigger": {"kind": "stop", "direction": "above", "reference": "bar high", "price_field": "high", "price": "101", "source": source},
        "invalidation": {"reference": "bar low", "price_field": "low", "price": "99", "source": source},
        "evidence_for": ["breakout"], "evidence_against": ["near resistance"],
        "qualitative_confidence": "medium", "uncertainty": ["follow through"],
        "conditions_that_change_market_read": ["breakout fails"],
    }


def decision() -> dict:
    return {
        "schema": "brooks.management-decision.v2", "role": "POSITION_MANAGER",
        "decision_time_ms": 3_600_000, "action": "HOLD", "position_ids": ["main-1"],
        "reason": "thesis remains intact",
        "evidence": {"observations": ["position open"], "evidence_for": ["trend intact"], "evidence_against": ["volatility"]},
        "risk": {"exposure_before": ["main long"], "exposure_after": ["main long"], "protection_status": "adequate", "costs_considered": ["fees"], "uncertainty": "medium"},
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": None, "market_analysis_request": None,
        "conditions_that_change_action": ["breakdown"],
    }


def hedge_plan() -> dict:
    return {
        "objective": "reduce net exposure", "target_hedge_ratio": "0.30",
        "main_position_id": "main-1", "hedge_position_id": None,
        "ratio_basis": "absolute_mark_notional", "expected_effect_on_exposure": "lower net long",
        "costs": ["fees"], "unlock_condition": "weakness persists", "failure_condition": "reversal",
    }


def test_trader_v2_accepts_entry_and_no_trade():
    assert TradeIntentV2.model_validate(intent()).decision == "ENTER_LONG"
    abstain = intent()
    abstain.update(decision="NO_TRADE", decision_timeframe=None, entry_mechanism="none", trigger=None, invalidation=None)
    assert TradeIntentV2.model_validate(abstain).decision == "NO_TRADE"


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(extra="forbidden"),
    lambda x: x.update(role="POSITION_MANAGER"),
    lambda x: x.update(decision_time_ms="3599999"),
    lambda x: x.update(market_context={"nested": {"equity": "100"}}),
    lambda x: x["trigger"].update(price="98"),
    lambda x: x["trigger"]["source"].update(bar_index=-1),
])
def test_trader_rejects_invalid_or_private_fields(mutate):
    payload = intent()
    mutate(payload)
    with pytest.raises(ValidationError):
        TradeIntentV2.model_validate(payload)


def test_management_actions_and_hedge_ratios():
    assert ManagementDecisionV2.model_validate(decision()).action == "HOLD"
    payload = decision()
    payload.update(action="HEDGE", hedge_plan=hedge_plan())
    assert ManagementDecisionV2.model_validate(payload).hedge_plan.target_hedge_ratio == "0.30"
    for bad in ("1.01", "-0.1", "-0", "1e-1", "NaN", 0.3):
        bad_plan = hedge_plan()
        bad_plan["target_hedge_ratio"] = bad
        with pytest.raises(ValidationError):
            HedgePlanV2.model_validate(bad_plan)


def test_management_rejects_unsupported_actions_and_llm_orders():
    payload = decision()
    payload["action"] = "MOVE_PROTECTION"
    with pytest.raises(ValidationError):
        ManagementDecisionV2.model_validate(payload)
    payload = decision()
    payload["execution"]["orders"] = [{"side": "SELL"}]
    with pytest.raises(ValidationError):
        ManagementDecisionV2.model_validate(payload)
    payload = decision()
    payload.update(action="REMOVE_HEDGE", hedge_plan={**hedge_plan(), "hedge_position_id": "hedge-1"})
    with pytest.raises(ValidationError):
        ManagementDecisionV2.model_validate(payload)


def test_market_analysis_request_excludes_private_context():
    request = {"schema": "brooks.market-analysis-request.v1", "request_id": "r1", "symbol": "BTC-USDT", "decision_time_ms": 1, "timeframes": ["H1"], "market_fields": ["ordered_ohlc"]}
    assert MarketAnalysisRequestV1.model_validate(request).request_id == "r1"
    request["position_side"] = "LONG"
    with pytest.raises(ValidationError):
        MarketAnalysisRequestV1.model_validate(request)


def test_pm_account_decimals_are_canonical():
    from tests.test_brooks_market_tools import snapshot

    payload = snapshot().model_dump()
    payload["account"]["equity"] = "NaN"
    with pytest.raises(ValidationError):
        PositionManagementInputV2.model_validate(payload)
