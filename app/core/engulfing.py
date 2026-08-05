"""H1 engulfing candle — precise definition (PDF §6.5).

An engulfing completely engulfs the previous candle INCLUDING its entire
wick, confirmed by a BODY CLOSE beyond the previous candle's extreme — never
by a wick alone.

- Bullish: previous candle bearish; final bullish candle closes its body
  above the previous candle's wick HIGH.
- Bearish: previous candle bullish; final bearish candle closes its body
  below the previous candle's wick LOW.
- Type 1: a single candle completes the engulf.
- Type 2: two or more CONSECUTIVE SAME-COLOUR candles complete it; only the
  final one closes beyond the previous candle's wick. Mixed colours invalid.

The engulfing ZONE (order block) is always the engulfed (previous) candle —
its full wick-to-wick range — never the engulfing candle(s).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.models import Candle


@dataclass(frozen=True)
class Engulfing:
    direction: str        # 'BULLISH' | 'BEARISH'
    type: int             # 1 or 2
    engulfed: Candle      # the previous candle = the zone / order block
    final: Candle         # the candle whose body close confirmed it
    run_length: int       # 1 for Type 1, ≥2 for Type 2


def detect_engulfing(candles: list[Candle], direction: str) -> Engulfing | None:
    """Did the LAST candle in `candles` complete an engulfing in `direction`?

    Walks back through the trailing run of same-colour candles to the candle
    immediately before the run (the engulfed candle) and checks the body-close
    rule. Returns None if the last candle isn't the confirming close.
    """
    if len(candles) < 2:
        return None

    final = candles[-1]
    if direction == "BULLISH":
        if not final.bullish:
            return None
    elif direction == "BEARISH":
        if not final.bearish:
            return None
    else:
        raise ValueError(f"unknown direction: {direction}")

    same = (lambda c: c.bullish) if direction == "BULLISH" else (lambda c: c.bearish)

    # Trailing run of consecutive same-colour candles ending at `final`.
    i = len(candles) - 1
    while i >= 0 and same(candles[i]):
        i -= 1
    if i < 0:
        return None  # no previous candle to engulf
    engulfed = candles[i]
    run_length = len(candles) - 1 - i

    # The engulfed candle must be opposite-coloured (§6.5 definition).
    if direction == "BULLISH" and not engulfed.bearish:
        return None
    if direction == "BEARISH" and not engulfed.bullish:
        return None

    # Body close beyond the engulfed candle's FULL wick.
    if direction == "BULLISH":
        completed = final.close > engulfed.high
        # Only the FINAL candle may complete it: if an earlier candle in the
        # run had already closed beyond, the engulf confirmed back then.
        prior_completed = any(c.close > engulfed.high for c in candles[i + 1 : -1])
    else:
        completed = final.close < engulfed.low
        prior_completed = any(c.close < engulfed.low for c in candles[i + 1 : -1])

    if not completed or prior_completed:
        return None

    return Engulfing(
        direction=direction,
        type=1 if run_length == 1 else 2,
        engulfed=engulfed,
        final=final,
        run_length=run_length,
    )
