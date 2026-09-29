import asyncio
import logging
import math

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"


class Config(BaseModel):
    """15-minute multi-timeframe analysis (15m, 1h, 4h) for high-frequency adaptive grid."""

    trading_pair: str = Field(default="BTC-USDT")
    connector_name: str = Field(default="binance_perpetual_demo")
    lifetime_hours: float = Field(
        default=3.0,
        description="Expected active grid horizon (3h typical for 15-min cadence)",
    )
    atr_period: int = Field(default=14)
    baseline_atr: float = Field(
        default=0.0,
        description="From baseline_7d — 0 means compute from 15m candles only",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_candles(result) -> list:
    """Defensively parse candles from API response."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return result.get("data", result.get("candles", []))


def _compute_ema(closes: list, period: int) -> list:
    """Compute EMA series for a list of close prices."""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def _compute_atr(candles: list, period: int) -> float:
    """Compute ATR(period) from candle dicts."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i].get("high", 0) or 0)
        low = float(candles[i].get("low", 0) or 0)
        prev_close = float(candles[i - 1].get("close", 0) or 0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    if len(trs) < period:
        return sum(trs) / len(trs)
    # Wilder's smoothed ATR
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _trend_direction(candles: list, fast: int = 9, slow: int = 21, threshold_pct: float = 0.2) -> str:
    """Determine trend via EMA crossover (fast / slow). Returns BULLISH / BEARISH / NEUTRAL."""
    if len(candles) < slow + 2:
        return "NEUTRAL"
    closes = [float(c.get("close", 0) or 0) for c in candles]
    fast_ema = _compute_ema(closes, fast)
    slow_ema = _compute_ema(closes, slow)
    if not fast_ema or not slow_ema:
        return "NEUTRAL"
    diff_pct = (fast_ema[-1] - slow_ema[-1]) / slow_ema[-1] * 100 if slow_ema[-1] else 0
    if diff_pct > threshold_pct:
        return "BULLISH"
    elif diff_pct < -threshold_pct:
        return "BEARISH"
    return "NEUTRAL"


def _volatility_level(atr: float, candles_window: list) -> tuple:
    """Return (vol_level, range_high, range_low, range_pct, range_position)."""
    highs = [float(c.get("high", 0) or 0) for c in candles_window]
    lows = [float(c.get("low", 0) or 0) for c in candles_window]
    closes = [float(c.get("close", 0) or 0) for c in candles_window]
    range_high = max(highs) if highs else 0.0
    range_low = min(lows) if lows else 0.0
    current = closes[-1] if closes else 0.0
    range_size = range_high - range_low
    range_pct = (range_size / current * 100) if current else 0.0
    range_pos = (current - range_low) / range_size if range_size > 0 else 0.5
    avg_candle_range = range_size / len(candles_window) if candles_window else 1.0
    vol_ratio = atr / avg_candle_range if avg_candle_range else 1.0
    if vol_ratio > 1.4:
        vol_level = "HIGH"
    elif vol_ratio > 0.7:
        vol_level = "MODERATE"
    else:
        vol_level = "LOW"
    return vol_level, range_high, range_low, range_pct, range_pos


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return "No server available"

    # -- 1. Fetch 15m, 1h, and 4h in parallel --
    try:
        raw_15m, raw_1h, raw_4h = await asyncio.gather(
            client.market_data.get_candles(
                config.connector_name, config.trading_pair, "15m", max_records=60
            ),
            client.market_data.get_candles(
                config.connector_name, config.trading_pair, "1h", max_records=50
            ),
            client.market_data.get_candles(
                config.connector_name, config.trading_pair, "4h", max_records=50
            ),
        )
    except Exception as e:
        logger.error(f"[mtf_15m_check] candle fetch failed: {e}")
        return f"Error fetching market data: {e}"

    candles_15m = _parse_candles(raw_15m)
    candles_1h = _parse_candles(raw_1h)
    candles_4h = _parse_candles(raw_4h)

    if not candles_15m:
        return "No 15m candle data available — cannot proceed."

    # -- 2. Micro and Macro analysis --
    atr_15m = _compute_atr(candles_15m, config.atr_period)
    window_24 = candles_15m[-24:] if len(candles_15m) >= 24 else candles_15m
    vol_level, range_high, range_low, range_pct, range_pos = _volatility_level(
        atr_15m, window_24
    )
    current_price = float(candles_15m[-1].get("close", 0) or 0)

    trend_15m = _trend_direction(candles_15m, fast=9, slow=21, threshold_pct=0.15)
    trend_1h = _trend_direction(candles_1h, fast=9, slow=21, threshold_pct=0.25) if candles_1h else "NEUTRAL"
    trend_4h = _trend_direction(candles_4h, fast=9, slow=21, threshold_pct=0.30) if candles_4h else "NEUTRAL"

    # -- 3. Signal synthesis --
    # MT4 Hedging Grid bias:
    # If 1h and 4h are BULLISH or 15m+1h BULLISH -> LONG_GRID
    # If 1h and 4h are BEARISH -> SHORT_GRID
    # In range or mixed signals with low/mod vol -> TWO_SIDED_GRID
    if (trend_1h == "BULLISH" and trend_4h == "BULLISH") or (trend_15m == "BULLISH" and trend_1h == "BULLISH"):
        profile = "LONG_GRID"
        rationale = "Bullish momentum aligned across 15m/1h/4h. Skew grid upward."
    elif (trend_1h == "BEARISH" and trend_4h == "BEARISH") or (trend_15m == "BEARISH" and trend_1h == "BEARISH"):
        profile = "SHORT_GRID"
        rationale = "Bearish pressure aligned across 15m/1h/4h. Skew grid downward (max 2 locks enabled)."
    elif vol_level in ("LOW", "MODERATE"):
        profile = "TWO_SIDED_GRID"
        rationale = "Range-bound oscillation across 15m timeframe. Symmetric hedging grid."
    else:
        profile = "TWO_SIDED_GRID"  # In aggressive grid mode, keep two-sided rather than flat HOLD
        rationale = "Volatile market conditions. Hedging locks will manage boundary excursions."

    # -- 4. Dynamic Grid Boundaries (15m cadence) --
    D = atr_15m * math.sqrt(config.lifetime_hours)
    # Ensure minimum spread
    spread_pct = max(0.0015, (atr_15m / current_price) * 0.5)

    if profile == "LONG_GRID":
        start_price = current_price - 1.5 * D
        end_price = current_price + 2.5 * D
        limit_price = current_price - 2.0 * D
        lock_trigger_price = current_price - 1.0 * D
    elif profile == "SHORT_GRID":
        start_price = current_price - 2.5 * D
        end_price = current_price + 1.5 * D
        limit_price = current_price + 2.0 * D
        lock_trigger_price = current_price + 1.0 * D
    else:  # TWO_SIDED_GRID
        start_price = current_price - 2.0 * D
        end_price = current_price + 2.0 * D
        limit_price = current_price - 2.5 * D
        lock_trigger_price = current_price - 1.2 * D

    output = [
        f"**15-Minute MTF Check — {config.trading_pair}**",
        f"- Current Price: **${current_price:,.2f}**",
        f"- ATR(15m): **${atr_15m:,.2f}** (volatility: {vol_level})",
        f"- 6h Range: ${range_low:,.2f} – ${range_high:,.2f} ({range_pct:.2f}%, position {range_pos:.1%})",
        f"- Trends: 15m={trend_15m} | 1h={trend_1h} | 4h={trend_4h}",
        f"- **Recommendation:** **{profile}**",
        f"- Rationale: {rationale}",
        "",
        "**Aggressive 10-Level Grid Parameters (100x):**",
        f"- Start Price: ${start_price:,.2f}",
        f"- End Price: ${end_price:,.2f}",
        f"- Invalidation / Limit: ${limit_price:,.2f}",
        f"- Hedging Lock Trigger (Trava): ${lock_trigger_price:,.2f}",
        f"- Recommended Level Spacing: {spread_pct * 100:.3f}% (~${current_price * spread_pct:,.2f})",
        f"- Recommended Take-Profit: 0.25% - 0.40%",
        f"- Position Capacity: up to 10 per side (max 20 total)",
    ]

    return "\n".join(output)
