"""Configuration for the Brooks runtime, independent of legacy tick cadence."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MarketWakeConfig(BaseModel):
    timeframe: Literal["1h", "1d"]
    wake_offset_sec: int = Field(ge=0, lt=3600)


class PeriodicWakeConfig(BaseModel):
    frequency_sec: int = Field(gt=0)


class BrooksConfig(BaseModel):
    trader: MarketWakeConfig = Field(
        default_factory=lambda: MarketWakeConfig(timeframe="1h", wake_offset_sec=2)
    )
    htf: MarketWakeConfig = Field(
        default_factory=lambda: MarketWakeConfig(timeframe="1d", wake_offset_sec=3)
    )
    pm: PeriodicWakeConfig = Field(
        default_factory=lambda: PeriodicWakeConfig(frequency_sec=60)
    )
    position_watcher: PeriodicWakeConfig = Field(
        default_factory=lambda: PeriodicWakeConfig(frequency_sec=10)
    )

    @classmethod
    def from_engine_config(cls, config: dict[str, Any]) -> BrooksConfig:
        return cls.model_validate(config.get("brooks") or {})
