"""Versioned, fail-closed Brooks wire contracts.

The v2 Trader and PM shapes follow the brooks-harness/Skill-Brooks contracts.
Account data is deliberately absent from market-only contracts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


NonEmpty = StrictStr
Timestamp = StrictInt
DecimalText = StrictStr


def _decimal(value: str, *, ratio: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("expected canonical decimal string")
    import re

    if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value):
        raise ValueError("expected canonical decimal string")
    number = Decimal(value)
    if ratio and (value.startswith("-") or not Decimal(0) <= number <= Decimal(1)):
        raise ValueError("ratio must be between 0 and 1")
    return number


def _nonempty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


_PRIVATE_KEYS = frozenset({
    "account", "balance", "equity", "available_margin", "margin", "leverage",
    "fees", "funding", "positions", "position", "position_state", "open_positions",
    "open_orders", "orders", "fills", "entry_price", "entry_prices", "position_side",
    "position_size", "quantity", "unrealized_pnl", "realized_pnl", "pnl",
    "trade_history", "hedge", "hedges", "management_history", "pm_intent",
    "portfolio", "future_bars", "outcome_bars", "future_observation", "final_pnl",
    "profitability", "realized_return", "mfe", "mae",
})


def _check_public(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _PRIVATE_KEYS:
                raise ValueError(f"private or future market field: {key}")
            _check_public(child)
    elif isinstance(value, list):
        for child in value:
            _check_public(child)


class TriggerSource(Contract):
    timeframe: NonEmpty
    bar_index: StrictInt = Field(ge=0)
    open_time_ms: Timestamp = Field(ge=0)
    close_time_ms: Timestamp = Field(ge=0)

    _name = field_validator("timeframe")(_nonempty)


class PriceReference(Contract):
    reference: NonEmpty
    price_field: Literal["open", "high", "low", "close"]
    price: DecimalText
    source: TriggerSource

    _reference = field_validator("reference")(_nonempty)

    @field_validator("price")
    @classmethod
    def valid_price(cls, value: str) -> str:
        _decimal(value)
        return value


class Trigger(PriceReference):
    kind: Literal["market", "stop", "limit"]
    direction: Literal["at", "above", "below"]


class Setup(Contract):
    type: StrictStr | None = None
    trigger_status: Literal["pending", "triggered", "present", "absent", "failed", "stale", "unknown"] | None = None
    signal_quality: Literal["clear", "weak", "failed", "absent", "unknown"] | None = None
    location_assessment: Literal["favorable", "neutral", "poor", "unknown"] | None = None
    no_trade_reason: Literal["no_trigger", "poor_location", "weak_signal", "failed_breakout", "opposing_pressure", "insufficient_data", "timeframe_conflict", "stale_or_late", "missing_invalidation", "balanced_context"] | None = None


class TradeIntentV2(Contract):
    schema: Literal["brooks.trade-intent.v2"]
    role: Literal["TRADER"]
    decision: Literal["ENTER_LONG", "ENTER_SHORT", "NO_TRADE"]
    symbol: NonEmpty
    decision_time_ms: Timestamp = Field(ge=0)
    market_context: dict[str, Any] | None
    setup: Setup | None
    decision_timeframe: StrictStr | None
    context_timeframes_used: list[NonEmpty]
    entry_mechanism: Literal["continuation", "breakout", "breakout_pullback", "reversal", "none", "unclear"]
    trigger: Trigger | None
    invalidation: PriceReference | None
    evidence_for: list[NonEmpty] = Field(min_length=1)
    evidence_against: list[NonEmpty] = Field(min_length=1)
    qualitative_confidence: Literal["high", "medium", "low"]
    uncertainty: list[NonEmpty] = Field(min_length=1)
    conditions_that_change_market_read: list[NonEmpty] = Field(min_length=1)

    @field_validator("symbol", "decision_timeframe")
    @classmethod
    def nonempty_if_present(cls, value: str | None) -> str | None:
        return _nonempty(value) if value is not None else value

    @field_validator("context_timeframes_used", "evidence_for", "evidence_against", "uncertainty", "conditions_that_change_market_read")
    @classmethod
    def nonempty_items(cls, values: list[str]) -> list[str]:
        for value in values:
            _nonempty(value)
        return values

    @model_validator(mode="after")
    def semantics(self) -> TradeIntentV2:
        _check_public(self.market_context)
        if self.decision == "NO_TRADE":
            if self.decision_timeframe is not None or self.trigger is not None or self.invalidation is not None:
                raise ValueError("NO_TRADE cannot include decision timeframe, trigger or invalidation")
            if self.entry_mechanism not in ("none", "unclear") and not (self.setup and self.setup.no_trade_reason):
                raise ValueError("active mechanism requires no_trade_reason")
        else:
            if not self.decision_timeframe or not self.trigger or not self.invalidation:
                raise ValueError("entry requires timeframe, trigger and invalidation")
            if self.entry_mechanism in ("none", "unclear"):
                raise ValueError("entry requires concrete mechanism")
            if self.setup and self.setup.no_trade_reason:
                raise ValueError("entry cannot have no_trade_reason")
            if self.setup and self.setup.trigger_status == "pending" and self.trigger.kind == "market":
                raise ValueError("pending trigger must be stop or limit")
            trigger = _decimal(self.trigger.price)
            invalidation = _decimal(self.invalidation.price)
            if self.decision == "ENTER_LONG" and trigger <= invalidation:
                raise ValueError("long trigger must exceed invalidation")
            if self.decision == "ENTER_SHORT" and trigger >= invalidation:
                raise ValueError("short trigger must be below invalidation")
        return self


class MarketContextV1(Contract):
    schema: Literal["brooks.market-context.v1"]
    role: Literal["HTF_ANALYST"]
    symbol: NonEmpty
    decision_time_ms: Timestamp = Field(ge=0)
    timeframe: NonEmpty
    observations: list[NonEmpty] = Field(min_length=1)
    evidence_against: list[NonEmpty] = Field(min_length=1)
    uncertainty: list[NonEmpty] = Field(min_length=1)

    @model_validator(mode="after")
    def public_only(self) -> MarketContextV1:
        _check_public(self.model_dump())
        for value in (self.symbol, self.timeframe, *self.observations, *self.evidence_against, *self.uncertainty):
            _nonempty(value)
        return self


class ContextStructure(Contract):
    """A bounded, descriptive market structure reported by a context analyst."""

    kind: Literal[
        "range", "channel", "breakout", "mtr-like", "climax", "two-sided", "swing", "other"
    ]
    description: NonEmpty
    start_time_ms: Timestamp | None = Field(default=None, ge=0)
    end_time_ms: Timestamp | None = Field(default=None, ge=0)
    upper_boundary: DecimalText | None = None
    lower_boundary: DecimalText | None = None

    @field_validator("description")
    @classmethod
    def description_nonempty(cls, value: str) -> str:
        return _nonempty(value)

    @field_validator("upper_boundary", "lower_boundary")
    @classmethod
    def valid_boundary(cls, value: str | None) -> str | None:
        if value is not None:
            if _decimal(value) <= 0:
                raise ValueError("structure boundaries must be positive prices")
        return value

    @model_validator(mode="after")
    def ordered_bounds(self) -> ContextStructure:
        if (
            self.start_time_ms is not None
            and self.end_time_ms is not None
            and self.end_time_ms < self.start_time_ms
        ):
            raise ValueError("structure end precedes its start")
        if (
            self.upper_boundary is not None
            and self.lower_boundary is not None
            and _decimal(self.upper_boundary) < _decimal(self.lower_boundary)
        ):
            raise ValueError("structure upper boundary is below lower boundary")
        return self


class MarketContextV2(Contract):
    """Market-only structural description for a single D1 or H4 window.

    Deliberately has no recommendation, side, target, stop, quantity, or
    probability field. ``window_bars == 0`` is reserved for a conservative
    legacy V1 read where the original window size was not recorded.
    """

    schema: Literal["brooks.market-context.v2"]
    role: Literal["CONTEXT_ANALYST"]
    symbol: NonEmpty
    timeframe: Literal["D1", "H4"]
    decision_time_ms: Timestamp = Field(ge=0)
    window_bars: StrictInt = Field(ge=0, le=120)
    primary_regime: Literal["bull-trend", "bear-trend", "trading-range", "transition-unclear"]
    phase: Literal["breakout-spike", "channel", "range", "transition", "unclear"]
    breakout_mode: StrictBool | Literal["unclear"]
    directional_pressure: Literal["bull", "bear", "balanced", "unclear"]
    always_in: Literal["long", "short", "unclear"]
    always_in_relevance: Literal["high", "medium", "low"]
    observations: list[NonEmpty] = Field(min_length=1)
    structures: list[ContextStructure]
    evidence_for: list[NonEmpty] = Field(min_length=1)
    evidence_against: list[NonEmpty] = Field(min_length=1)
    transition_conditions: list[NonEmpty] = Field(min_length=1)
    missing_information: list[NonEmpty]
    confidence: Literal["high", "medium", "low"]

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        return _nonempty(value)

    @field_validator(
        "observations",
        "evidence_for",
        "evidence_against",
        "transition_conditions",
        "missing_information",
    )
    @classmethod
    def valid_text_lists(cls, values: list[str]) -> list[str]:
        for value in values:
            _nonempty(value)
        return values

    @model_validator(mode="after")
    def public_market_only(self) -> MarketContextV2:
        _check_public(self.model_dump())
        if self.window_bars not in (0, 120):
            raise ValueError("context analyst window must contain exactly 120 bars")
        if self.window_bars == 0 and not self.missing_information:
            raise ValueError("unknown legacy window size must be disclosed")
        if any(
            timestamp is not None and timestamp > self.decision_time_ms
            for structure in self.structures
            for timestamp in (structure.start_time_ms, structure.end_time_ms)
        ):
            raise ValueError("context structure cannot extend beyond decision_time_ms")
        return self


class MarketAnalysisRequestV1(Contract):
    schema: Literal["brooks.market-analysis-request.v1"]
    request_id: NonEmpty
    symbol: NonEmpty
    decision_time_ms: Timestamp = Field(ge=0)
    timeframes: list[NonEmpty] = Field(min_length=1)
    market_fields: list[Literal["ordered_ohlc", "bar_by_bar", "decision_time"]] = Field(min_length=1)


class MarketAnalysisResponseV1(Contract):
    schema: Literal["brooks.market-analysis-response.v1"]
    request_id: NonEmpty
    symbol: NonEmpty
    decision_time_ms: Timestamp = Field(ge=0)
    trade_intent: TradeIntentV2


class AccountState(Contract):
    balance: DecimalText
    equity: DecimalText
    available_margin: DecimalText
    realized_pnl: DecimalText | None = None
    fees: DecimalText | dict[str, Any]
    funding: DecimalText
    currency: NonEmpty
    leverage: DecimalText
    position_mode: NonEmpty

    @field_validator("balance", "equity", "available_margin", "realized_pnl", "funding", "leverage")
    @classmethod
    def decimals(cls, value: str | None) -> str | None:
        if value is not None:
            _decimal(value)
        return value

    @field_validator("fees")
    @classmethod
    def valid_fees(cls, value: str | dict[str, Any]) -> str | dict[str, Any]:
        if isinstance(value, str):
            _decimal(value)
        return value


class StopProtectionState(Contract):
    required_by_policy: StrictBool
    covered_quantity: DecimalText
    uncovered_quantity: DecimalText
    policy_compliant: StrictBool

    @field_validator("covered_quantity", "uncovered_quantity")
    @classmethod
    def decimals(cls, value: str) -> str:
        _decimal(value)
        return value


class RiskControlState(Contract):
    account_risk_status: Literal["HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"]
    margin_ratio: DecimalText | None = None
    liquidation_distance_pct: DecimalText | None = None

    @field_validator("margin_ratio", "liquidation_distance_pct")
    @classmethod
    def decimals(cls, value: str | None) -> str | None:
        if value is not None:
            _decimal(value)
        return value


class PositionRecordV2(Contract):
    position_id: NonEmpty
    symbol: NonEmpty
    side: Literal["LONG", "SHORT"]
    quantity: DecimalText
    entry_price: DecimalText
    mark_price: DecimalText
    unrealized_pnl: DecimalText
    protective_order_ids: list[StrictStr]
    hedge_group_id: StrictStr | None = None
    ownership_role: Literal["MAIN", "HEDGE", "UNRESOLVED", "PROTECTIVE"] | None
    as_of_ms: Timestamp = Field(ge=0)
    management_policy_id: StrictStr | None = None
    stop_protection: StopProtectionState | None = None
    risk_control: RiskControlState | None = None

    @field_validator("quantity", "entry_price", "mark_price", "unrealized_pnl")
    @classmethod
    def decimals(cls, value: str) -> str:
        _decimal(value)
        return value


class OpenOrderRecord(Contract):
    order_id: NonEmpty
    symbol: NonEmpty
    position_id: NonEmpty
    side: Literal["LONG", "SHORT"]
    order_type: NonEmpty
    status: Literal["OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN"]
    quantity: DecimalText
    filled_quantity: DecimalText
    reduce_only: StrictBool
    price: DecimalText | None
    stop_price: DecimalText | None
    as_of_ms: Timestamp = Field(ge=0)

    @field_validator("quantity", "filled_quantity", "price", "stop_price")
    @classmethod
    def decimals(cls, value: str | None) -> str | None:
        if value is not None:
            _decimal(value)
        return value


class FillRecord(Contract):
    fill_id: NonEmpty
    order_id: NonEmpty
    position_id: NonEmpty
    symbol: NonEmpty
    side: Literal["LONG", "SHORT"]
    quantity: DecimalText
    price: DecimalText
    fee: DecimalText
    funding: DecimalText
    filled_at_ms: Timestamp = Field(ge=0)

    @field_validator("quantity", "price", "fee", "funding")
    @classmethod
    def decimals(cls, value: str) -> str:
        _decimal(value)
        return value


class ManagementHistoryRecord(Contract):
    event_id: NonEmpty
    action: NonEmpty
    position_ids: list[StrictStr]
    at_ms: Timestamp = Field(ge=0)


class ManagementPolicyContext(Contract):
    policy_id: NonEmpty
    version: NonEmpty
    policy_family: NonEmpty
    strategy_stop_required: StrictBool
    allowed_management_actions: list[NonEmpty]
    protection_semantics: NonEmpty
    applicable_risk_behavior: dict[str, Any] | None = None


class HedgeStateV1(Contract):
    schema: Literal["condor.brooks.hedge-state.v1"] = "condor.brooks.hedge-state.v1"
    main_side: Literal["LONG", "SHORT"] | None
    main_size: DecimalText
    hedge_side: Literal["LONG", "SHORT"] | None
    hedge_size: DecimalText
    net_exposure: DecimalText
    hedge_ratio: DecimalText
    main_position_id: StrictStr | None
    hedge_position_id: StrictStr | None
    unresolved: StrictBool
    structure_status: Literal["ok", "single_main", "orphan_hedge", "unknown_role", "duplicate_main", "duplicate_hedge", "inconsistent_ownership", "no_positions"]
    net_exposure_usd: DecimalText
    gross_exposure_usd: DecimalText
    ratio_basis: Literal["absolute_mark_notional"] = "absolute_mark_notional"

    @field_validator("hedge_ratio")
    @classmethod
    def valid_ratio(cls, value: str) -> str:
        _decimal(value, ratio=True)
        return value

    @field_validator("main_size", "hedge_size", "net_exposure", "net_exposure_usd", "gross_exposure_usd")
    @classmethod
    def decimals(cls, value: str) -> str:
        _decimal(value)
        return value


class PositionManagementInputV2(Contract):
    schema: Literal["brooks.position-management-input.v2"]
    role: Literal["POSITION_MANAGER"]
    decision_time_ms: Timestamp = Field(ge=0)
    account: AccountState
    positions: list[PositionRecordV2]
    open_orders: list[OpenOrderRecord]
    fills_since_last_event: list[FillRecord]
    management_history: list[ManagementHistoryRecord]
    market_analysis: MarketAnalysisResponseV1 | None
    management_policy: ManagementPolicyContext
    hedge_state: HedgeStateV1
    margin_health: Literal["SAFE", "WARNING", "CRITICAL"]
    original_trade_intent: TradeIntentV2 | None = None
    latest_trader_intent: TradeIntentV2 | None = None
    latest_market_context: MarketContextV1 | None = None


class HedgePlanV2(Contract):
    objective: NonEmpty
    target_hedge_ratio: DecimalText
    main_position_id: NonEmpty
    hedge_position_id: StrictStr | None
    ratio_basis: Literal["absolute_mark_notional"]
    expected_effect_on_exposure: NonEmpty
    costs: list[NonEmpty] = Field(min_length=1)
    unlock_condition: NonEmpty
    failure_condition: NonEmpty

    @field_validator("target_hedge_ratio")
    @classmethod
    def valid_ratio(cls, value: str) -> str:
        _decimal(value, ratio=True)
        return value


class ManagementEvidence(Contract):
    observations: list[NonEmpty] = Field(min_length=1)
    evidence_for: list[NonEmpty] = Field(min_length=1)
    evidence_against: list[NonEmpty] = Field(min_length=1)


class ManagementRisk(Contract):
    exposure_before: list[NonEmpty] = Field(min_length=1)
    exposure_after: list[NonEmpty] = Field(min_length=1)
    protection_status: Literal["adequate", "inadequate", "unknown", "not_applicable"]
    costs_considered: list[NonEmpty] = Field(min_length=1)
    uncertainty: Literal["high", "medium", "low"]


class ManagementExecution(Contract):
    # V1 actions are GM compiled. The PM cannot supply executable orders.
    orders: list[Any] = Field(max_length=0)
    cancel_order_ids: list[Any] = Field(max_length=0)
    replace_orders: list[Any] = Field(max_length=0)


ManagementAction = Literal["HOLD", "REDUCE", "CLOSE", "HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE", "REQUEST_MARKET_ANALYSIS", "RECONCILE_STATE", "MANAGEMENT_BLOCKED"]
_HEDGE_ACTIONS = {"HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"}


class ManagementDecisionV2(Contract):
    schema: Literal["brooks.management-decision.v2"]
    role: Literal["POSITION_MANAGER"]
    decision_time_ms: Timestamp = Field(ge=0)
    action: ManagementAction
    position_ids: list[NonEmpty]
    reason: NonEmpty
    evidence: ManagementEvidence
    risk: ManagementRisk
    execution: ManagementExecution
    hedge_plan: HedgePlanV2 | None
    market_analysis_request: MarketAnalysisRequestV1 | None
    conditions_that_change_action: list[NonEmpty] = Field(min_length=1)
    reduce_fraction: DecimalText | None = None
    shadow_mode: StrictBool = False

    @field_validator("reduce_fraction")
    @classmethod
    def valid_reduce_fraction(cls, value: str | None) -> str | None:
        if value is not None:
            num = _decimal(value)
            if not (Decimal(0) < num < Decimal(1)):
                raise ValueError("reduce_fraction must be strictly between 0 and 1")
        return value

    @model_validator(mode="after")
    def action_requirements(self) -> ManagementDecisionV2:
        if self.action in _HEDGE_ACTIONS and self.hedge_plan is None:
            raise ValueError("hedge action requires hedge_plan")
        if self.action not in _HEDGE_ACTIONS and self.hedge_plan is not None:
            raise ValueError("hedge_plan only allowed for hedge actions")
        if self.action == "REMOVE_HEDGE" and self.hedge_plan and _decimal(self.hedge_plan.target_hedge_ratio) != 0:
            raise ValueError("REMOVE_HEDGE requires zero target ratio")
        if (self.action == "REQUEST_MARKET_ANALYSIS") != (self.market_analysis_request is not None):
            raise ValueError("market_analysis_request only for REQUEST_MARKET_ANALYSIS")
        if self.action == "REDUCE" and self.reduce_fraction is None:
            raise ValueError("REDUCE action requires reduce_fraction")
        if self.action != "REDUCE" and self.reduce_fraction is not None:
            raise ValueError("reduce_fraction only allowed for REDUCE action")
        if self.action in {"HOLD", "REDUCE", "CLOSE", *_HEDGE_ACTIONS} and not self.position_ids:
            raise ValueError("action requires position_ids")
        return self


class BrooksEventV1(Contract):
    schema: Literal["condor.brooks.event.v1"] = "condor.brooks.event.v1"
    event_id: NonEmpty
    type: NonEmpty
    created_at_ms: Timestamp = Field(ge=0)
    symbol: NonEmpty
    correlation_id: NonEmpty
    causation_id: StrictStr | None
    payload: dict[str, Any]


class GMDecisionV1(Contract):
    schema: Literal["condor.brooks.gm-decision.v1"] = "condor.brooks.gm-decision.v1"
    correlation_id: NonEmpty
    decision_time_ms: Timestamp = Field(ge=0)
    approved: StrictBool
    reason: NonEmpty
    command_id: StrictStr | None = None


class ExecutionCommandV1(Contract):
    schema: Literal["condor.brooks.execution-command.v1"] = "condor.brooks.execution-command.v1"
    command_id: NonEmpty
    correlation_id: NonEmpty
    symbol: NonEmpty
    action: Literal["OPEN_MAIN", "REDUCE_MAIN", "CLOSE_MAIN", "OPEN_HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"]
    quantity: DecimalText
    side: Literal["BUY", "SELL"]
    position_action: Literal["OPEN", "CLOSE"]

    @field_validator("quantity")
    @classmethod
    def positive_quantity(cls, value: str) -> str:
        if _decimal(value) <= 0:
            raise ValueError("quantity must be positive")
        return value


class TradeBindingV1(Contract):
    schema: Literal["condor.brooks.trade-binding.v1"] = "condor.brooks.trade-binding.v1"
    correlation_id: NonEmpty
    symbol: NonEmpty
    connector: NonEmpty
    account_id: NonEmpty
    main_position_id: NonEmpty
    main_executor_id: NonEmpty
    hedge_position_id: StrictStr | None = None
    created_at_ms: Timestamp = Field(ge=0)
