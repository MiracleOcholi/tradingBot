"""SNR detection engine (PDF §1–§5, verbatim).

- An SNR exists only after the SECOND candle closes; it is always marked at
  the FIRST candle's close and the marking point never moves.
- Formations: TRAD_R (bull→bear), TRAD_S (bear→bull), OC_R (bear→bear),
  OC_S (bull→bull). A doji on either candle → no formation.
- Role flips on BODY-CLOSE breaks only: S broken below → SBR (acts as R);
  R broken above → RBS (acts as S); a second break → Left Shoulder.
- Fresh = zero prior touches at first arrival; the qualifying tap itself does
  not disqualify. Any prior touch ⇒ tested.
- Touches/breaks are only counted from the THIRD candle onward — the two
  forming candles are part of the formation, not interactions with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.models import Candle, Formation, SNRLevel


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


ROLE_OF_FORMATION = {
    Formation.TRAD_R: "R",
    Formation.OC_R: "R",
    Formation.TRAD_S: "S",
    Formation.OC_S: "S",
}


@dataclass
class SNRUpdate:
    """Result of processing one completed candle through a tracker."""
    new_level: SNRLevel | None = None
    touched: list[SNRLevel] = field(default_factory=list)   # touches += 1 this candle
    flipped: list[SNRLevel] = field(default_factory=list)   # role flip (SBR/RBS/LS)
    deactivated: list[SNRLevel] = field(default_factory=list)


class SNRTracker:
    """Streams completed candles for one (symbol, timeframe); maintains levels.

    Stateless-service friendly: construct with levels loaded from Supabase and
    the last seen candle; every mutation is reported via SNRUpdate so the
    caller can persist it.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        granularity_s: int,
        levels: list[SNRLevel] | None = None,
        prev_candle: Candle | None = None,
        max_active: int = 150,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.granularity = timedelta(seconds=granularity_s)
        self.levels: list[SNRLevel] = list(levels or [])
        self.prev_candle = prev_candle
        self.max_active = max_active

    # ------------------------------------------------------------------
    def _second_candle_open(self, level: SNRLevel) -> datetime:
        return level.first_candle_at + self.granularity

    def _interacts(self, level: SNRLevel, candle: Candle) -> bool:
        """Candles at/before the formation's 2nd candle never interact."""
        return candle.ts > self._second_candle_open(level)

    # ------------------------------------------------------------------
    def process_candle(self, candle: Candle) -> SNRUpdate:
        """Feed the next COMPLETED candle. Order per candle:
        1) touches + body-close breaks against existing levels,
        2) new-formation detection with the previous candle."""
        upd = SNRUpdate()

        for level in self.levels:
            if not level.active or not self._interacts(level, candle):
                continue

            if candle.low <= level.price <= candle.high:
                level.touches += 1
                level.fresh = level.touches == 0
                upd.touched.append(level)

            broke = (
                (level.role == "S" and candle.close < level.price)
                or (level.role == "R" and candle.close > level.price)
            )
            if broke:
                level.role = "R" if level.role == "S" else "S"
                level.break_count += 1
                # A break is decisive interaction: the level is no longer fresh
                # even if the candle body jumped the line without wick contact.
                if level.touches == 0:
                    level.touches += 1
                    level.fresh = False
                    upd.touched.append(level)
                upd.flipped.append(level)

        if self.prev_candle is not None:
            formation = detect_new_snr(self.prev_candle, candle)
            if formation is not None:
                upd.new_level = SNRLevel(
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    price=self.prev_candle.close,      # 1st candle's close, always
                    formation=formation,
                    role=ROLE_OF_FORMATION[formation],
                    first_candle_at=self.prev_candle.ts,
                )
                self.levels.append(upd.new_level)

        active = [l for l in self.levels if l.active]
        if len(active) > self.max_active:
            for stale in sorted(active, key=lambda l: l.first_candle_at)[: len(active) - self.max_active]:
                stale.active = False
                upd.deactivated.append(stale)

        self.prev_candle = candle
        return upd

    # ------------------------------------------------------------------
    def active_levels(self) -> list[SNRLevel]:
        return [l for l in self.levels if l.active]

    def last_traditional(self, role: str, before: datetime | None = None) -> SNRLevel | None:
        """Most recent ACTIVE Traditional SNR currently holding `role`,
        formed strictly before `before` (used for the H4 direction break:
        'the last H4 Traditional SNR before the tap')."""
        best: SNRLevel | None = None
        for l in self.levels:
            if not l.active or l.role != role:
                continue
            if l.formation not in (Formation.TRAD_R, Formation.TRAD_S):
                continue
            if before is not None and l.first_candle_at >= before:
                continue
            if best is None or l.first_candle_at > best.first_candle_at:
                best = l
        return best
