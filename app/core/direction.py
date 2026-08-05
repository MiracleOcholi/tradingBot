"""Direction confirmation engine (PDF §6.1) — pure logic, no I/O.

Direction is confirmed by TWO events in sequence:
1. A Daily rejection of a FRESH Daily SNR (exact-line tap; rejection dies if
   an H4 or Daily candle body-closes beyond the level — same validity rule as
   the setup rejection, §6.6/§6.7 step 3).
2. An H4 body-to-body break of the LAST H4 *Traditional* SNR formed before
   the tap (Open–Close / SBR / RBS levels never qualify).

Bullish  = fresh Daily Support rejected  + H4 breaks last Traditional Resistance.
Bearish  = fresh Daily Resistance rejected + H4 breaks last Traditional Support.

Once confirmed, a direction stays valid until the MIRROR event — the same
two conditions on the opposite side — which this engine produces naturally:
an opposite candidate that completes simply flips the direction.

Taps are detected on M15 closes for arrival precision (the exact line must
trade); the tap logic only consumes levels that are fresh at that moment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.core.breaks import h4_body_to_body_break
from app.core.models import Candle, SNRLevel


@dataclass
class DirectionCandidate:
    side: str                  # 'BULLISH' | 'BEARISH' (what a completion confirms)
    level_id: str | None       # tapped fresh Daily SNR
    level_price: float
    level_role: str            # 'S' | 'R'
    tap_ts: datetime


@dataclass
class DirectionState:
    direction: str | None = None
    since: datetime | None = None
    candidate: DirectionCandidate | None = None


@dataclass
class DirectionEvent:
    kind: str                  # TAP | REJECT_INVALID | CONFIRMED | FLIPPED
    data: dict = field(default_factory=dict)


def _closes_beyond(close: float, level_price: float, level_role: str) -> bool:
    """Body close beyond a level: below a Support / above a Resistance."""
    return close < level_price if level_role == "S" else close > level_price


class DirectionEngine:
    def __init__(self, symbol: str, state: DirectionState | None = None) -> None:
        self.symbol = symbol
        self.state = state or DirectionState()

    # ---------------------------------------------------------------- taps
    def on_m15_close(self, candle: Candle, fresh_d1_levels: list[SNRLevel]) -> list[DirectionEvent]:
        """Watch for the exact-line tap of any fresh Daily SNR."""
        st = self.state
        if st.candidate is not None:
            return []
        for lvl in fresh_d1_levels:
            if not (lvl.fresh and lvl.active):
                continue
            if candle.low <= lvl.price <= candle.high:
                side = "BULLISH" if lvl.role == "S" else "BEARISH"
                if side == st.direction:
                    continue  # already pointing that way; only the mirror matters
                st.candidate = DirectionCandidate(
                    side=side,
                    level_id=lvl.id,
                    level_price=lvl.price,
                    level_role=lvl.role,
                    tap_ts=candle.ts,
                )
                return [DirectionEvent("TAP", {"side": side, "price": lvl.price})]
        return []

    # ---------------------------------------------------------------- H4
    def on_h4_close(
        self,
        candle: Candle,
        last_h4_traditional: Callable[[str, datetime], SNRLevel | None],
    ) -> list[DirectionEvent]:
        """Rejection-validity check, then the body-to-body break check.

        `last_h4_traditional(role, before)` must return the most recent H4
        Traditional SNR currently holding `role`, formed before `before`.
        """
        st = self.state
        cand = st.candidate
        if cand is None:
            return []

        if _closes_beyond(candle.close, cand.level_price, cand.level_role):
            st.candidate = None
            return [DirectionEvent("REJECT_INVALID", {"tf": "H4"})]

        ref_role = "R" if cand.side == "BULLISH" else "S"
        ref = last_h4_traditional(ref_role, cand.tap_ts)
        if ref is not None and h4_body_to_body_break(candle, ref.price, cand.side):
            flipped = st.direction is not None and st.direction != cand.side
            st.direction = cand.side
            st.since = candle.ts
            st.candidate = None
            kind = "FLIPPED" if flipped else "CONFIRMED"
            return [DirectionEvent(kind, {"direction": st.direction, "ref_price": ref.price})]
        return []

    # ---------------------------------------------------------------- D1
    def on_d1_close(self, candle: Candle) -> list[DirectionEvent]:
        st = self.state
        cand = st.candidate
        if cand is None:
            return []
        if _closes_beyond(candle.close, cand.level_price, cand.level_role):
            st.candidate = None
            return [DirectionEvent("REJECT_INVALID", {"tf": "D1"})]
        return []
