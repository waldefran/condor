"""Configuration for the Brooks runtime, independent of legacy tick cadence."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MarketWakeConfig(BaseModel):
    timeframe: Literal["1h", "4h", "1d"]
    wake_offset_sec: int = Field(ge=0, lt=3600)


class PeriodicWakeConfig(BaseModel):
    frequency_sec: int = Field(gt=0)


class BrooksGMPolicyConfig(BaseModel):
    """Deterministic GM risk policy; defaults mirror gm.GMPolicy."""

    risk_per_trade_pct: str = "0.01"
    max_positions: int = 2
    max_gross_exposure_pct: str = "2"
    leverage: int = 2
    take_profit_r: str = "2"
    time_limit_sec: int = 3600
    max_trigger_drift_pct: str = "0.01"
    max_snapshot_age_ms: int = 15_000
    max_intent_age_ms: int = 7_200_000


class BrooksConfig(BaseModel):
    trader: MarketWakeConfig = Field(
        default_factory=lambda: MarketWakeConfig(timeframe="1h", wake_offset_sec=2)
    )
    htf: MarketWakeConfig = Field(
        default_factory=lambda: MarketWakeConfig(timeframe="1d", wake_offset_sec=3)
    )
    h4: MarketWakeConfig = Field(
        default_factory=lambda: MarketWakeConfig(timeframe="4h", wake_offset_sec=3)
    )
    pm: PeriodicWakeConfig = Field(
        default_factory=lambda: PeriodicWakeConfig(frequency_sec=60)
    )
    position_watcher: PeriodicWakeConfig = Field(
        default_factory=lambda: PeriodicWakeConfig(frequency_sec=10)
    )
    # -- production venue binding (all optional; empty means inert) --
    # Venue symbols the clock publishes for (e.g. ["BTC-USDT"]).
    symbols: list[str] = Field(default_factory=list)
    # Live execution switch: the supervisor wires the Trader in shadow mode
    # (decisions persisted, zero writes) unless a run opts in explicitly.
    shadow_mode: bool = True
    # Hummingbot account/connector the Brooks lifecycle trades through.
    account_name: str = ""
    connector_name: str = ""
    # Executor owner tag; the engine defaults it to its own agent_id
    # (controller_id == agent_id, the executor-mode convention) when empty.
    controller_id: str = ""
    # Optional overrides; the engine otherwise resolves these from its own
    # top-level run config (agent_key) and server resolution (server_name).
    server_name: str | None = None
    agent_key: str | None = None
    trader_agent_key: str | None = None
    h4_agent_key: str | None = None
    d1_agent_key: str | None = None
    trader_timeout_sec: float = Field(default=360, gt=0)
    context_timeout_sec: float = Field(default=300, gt=0)
    max_role_attempts: int = Field(default=2, ge=1, le=2)
    retry_backoff_sec: float = Field(default=5, ge=0)
    gm: BrooksGMPolicyConfig = Field(default_factory=BrooksGMPolicyConfig)

    @classmethod
    def from_engine_config(cls, config: dict[str, Any]) -> BrooksConfig:
        return cls.model_validate(config.get("brooks") or {})
