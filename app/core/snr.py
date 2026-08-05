"""SNR detection engine — Phase B implementation target.

Contract (PDF §1–§5, verbatim):
- An SNR exists only after the SECOND candle closes; it is always marked at
  the FIRST candle's close and the marking point never moves.
- Formations: TRAD_R (bull→bear), TRAD_S (bear→bull), OC_R (bear→bear),
  OC_S (bull→bull).
- Role flips on BODY-CLOSE breaks only: S broken below → SBR (acts as R);
  R broken above → RBS (acts as S); a second break → Left Shoulder.
- Fresh = zero prior touches at first arrival; the qualifying tap itself does
  not disqualify. Any prior touch ⇒ tested.

Phase B fills in: detect_snr(candles) -> list[SNRLevel], apply_candle(level,
candle) role-flip/touch updates, and persistence via services.supabase.
"""
from __future__ import annotations

from app.core.models import Candle, Formation, SNRLevel  # noqa: F401


def detect_new_snr(prev: Candle, curr: Candle) -> Formation | None:
    """Classify the two most recent COMPLETED candles as an SNR formation.

    Returns the formation type or None. The level price is prev.close.
    """
    if prev.bullish and curr.bearish:
        return Formation.TRAD_R
    if prev.bearish and curr.bullish:
        return Formation.TRAD_S
    if prev.bearish and curr.bearish:
        return Formation.OC_R
    if prev.bullish and curr.bullish:
        return Formation.OC_S
    return None  # doji on either candle → no formation
