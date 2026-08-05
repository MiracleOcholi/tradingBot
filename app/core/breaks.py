"""Swing points and the two NON-interchangeable break definitions (PDF §6.4).

- H4 body-to-body break: an H4 BODY CLOSE beyond the body-close level (= the
  marked price) of the last H4 Traditional SNR.  → Direction confirmation.
- M15 wick-to-body break: an M15 BODY CLOSE beyond the WICK extreme of the
  last M15 swing (swing high for a bullish break, swing low for a bearish
  break).  → Setup structural break.
- Swing = extreme + ≥1 opposing candle (a swing high is a high followed by
  ≥1 bearish candle; a swing low is a low followed by ≥1 bullish candle).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.models import Candle


@dataclass(frozen=True)
class Swing:
    kind: str        # 'HIGH' | 'LOW'
    price: float     # wick extreme of the swing candle
    candle: Candle   # the candle holding the extreme
    confirmed_by: Candle  # the (first) opposing candle that confirmed it


def last_swing_high(candles: list[Candle]) -> Swing | None:
    """Most recent confirmed swing high: candle i's high with candles[i+1] bearish."""
    for i in range(len(candles) - 2, -1, -1):
        if candles[i + 1].bearish:
            return Swing("HIGH", candles[i].high, candles[i], candles[i + 1])
    return None


def last_swing_low(candles: list[Candle]) -> Swing | None:
    """Most recent confirmed swing low: candle i's low with candles[i+1] bullish."""
    for i in range(len(candles) - 2, -1, -1):
        if candles[i + 1].bullish:
            return Swing("LOW", candles[i].low, candles[i], candles[i + 1])
    return None


# ---------------------------------------------------------------- breaks
def h4_body_to_body_break(candle: Candle, level_price: float, direction: str) -> bool:
    """H4 body close beyond the marked (body-close) price of an H4 Traditional SNR.

    direction 'BULLISH' → close above a Traditional Resistance level;
    direction 'BEARISH' → close below a Traditional Support level.
    """
    if direction == "BULLISH":
        return candle.close > level_price
    if direction == "BEARISH":
        return candle.close < level_price
    raise ValueError(f"unknown direction: {direction}")


def m15_wick_to_body_break(candle: Candle, swing: Swing) -> bool:
    """M15 body close beyond the wick extreme of the last M15 swing.

    Bullish break: body close above a swing HIGH's wick.
    Bearish break: body close below a swing LOW's wick.
    """
    if swing.kind == "HIGH":
        return candle.close > swing.price
    return candle.close < swing.price
