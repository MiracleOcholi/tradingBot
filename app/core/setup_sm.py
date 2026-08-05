"""Setup state machine (PDF §8.3) with invalidations (§6.8) — pure logic.

IDLE → DIRECTION_SET → AWAIT_TAP → REJECTION_PENDING → M15_BREAK_PENDING →
H1_ENGULF_PENDING → SETUP_CONFIRMED, returning to IDLE on any invalidation.

Rule of Opposites: with a BEARISH direction we hunt a BULLISH retracement
(BUY at a fresh Daily Support); with a BULLISH direction, a bearish one
(SELL at a fresh Daily Resistance).

Implementation notes (kept faithful to §6.6–§6.9):
- AWAIT_TAP is 'watching the opposite fresh Daily SNR': we report it whenever
  a direction is set and at least one candidate level exists.
- REJECTION_PENDING is validated CONTINUOUSLY, not on a fixed clock: from the
  tap onward, any H4 or Daily body close beyond the level invalidates
  (§6.8-1/2); the M15 structural break may legitimately occur before the
  first post-tap H4 close (rule 4B is 'before any H4 close beyond'), so the
  machine watches for the M15 break immediately after a valid tap.
- The M15 swing to break is the last swing BEFORE the tap (worked example
  §7-B3: 'the last swing low ... before the rejection'); its confirming
  opposing candle may close after the tap.
- The order block is the swing candle whose wick extreme the M15 body close
  broke ('the candle engulfed by the M15 breakout', §6.9) — wick-to-wick.
- Entry = proximal edge of the order block (HANDOFF-V2): zone high for a
  BUY (price retraces down into it), zone low for a SELL.
- SL = beyond the strong high/low after the M15 break: the extreme confirmed
  swing since the break, falling back to the absolute extreme since the tap
  (the rejection peak) when no post-break swing is confirmed yet.
- H1 must print the engulfing and must NOT break structure (§6.5 box,
  §6.8-3): a body close beyond the last H1 swing wick invalidates, checked
  BEFORE engulfing detection on the same candle.
- On confirmation the tapped Daily SNR is 'played' — never fresh again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.breaks import last_swing_high, last_swing_low, m15_wick_to_body_break
from app.core.engulfing import detect_engulfing
from app.core.entry import TradePlan, build_plan
from app.core.models import Candle, SetupState, Side, SNRLevel


@dataclass
class SetupEvent:
    kind: str            # STATE | TAPPED | M15_BREAK | CONFIRMED | INVALIDATED
    data: dict = field(default_factory=dict)


@dataclass
class PendingSetup:
    side: Side                       # trade side of the retracement
    level_id: str | None             # tapped Daily SNR
    level_price: float
    level_role: str                  # 'S' | 'R'
    tap_ts: datetime
    swing_price: float | None = None # wick extreme to break
    swing_candle: dict | None = None # order-block candidate {o,h,l,c,ts}
    break_ts: datetime | None = None
    order_block: dict | None = None  # locked at break: {high, low, ts}

    def to_payload(self) -> dict:
        return {
            "side": self.side.value,
            "level_id": self.level_id,
            "level_price": self.level_price,
            "level_role": self.level_role,
            "tap_ts": self.tap_ts.isoformat(),
            "swing_price": self.swing_price,
            "swing_candle": self.swing_candle,
            "break_ts": self.break_ts.isoformat() if self.break_ts else None,
            "order_block": self.order_block,
        }

    @classmethod
    def from_payload(cls, p: dict) -> "PendingSetup":
        return cls(
            side=Side(p["side"]),
            level_id=p.get("level_id"),
            level_price=p["level_price"],
            level_role=p["level_role"],
            tap_ts=datetime.fromisoformat(p["tap_ts"]),
            swing_price=p.get("swing_price"),
            swing_candle=p.get("swing_candle"),
            break_ts=datetime.fromisoformat(p["break_ts"]) if p.get("break_ts") else None,
            order_block=p.get("order_block"),
        )


def _candle_dict(c: Candle) -> dict:
    return {"o": c.open, "h": c.high, "l": c.low, "c": c.close, "ts": c.ts.isoformat()}


def _closes_beyond(close: float, price: float, role: str) -> bool:
    return close < price if role == "S" else close > price


def compute_strong_extreme(
    m15: list[Candle], side: Side, break_ts: datetime, tap_ts: datetime
) -> float | None:
    """SL anchor: extreme confirmed swing since the M15 break; fallback to the
    absolute extreme since the tap (the rejection extreme)."""
    since_break = [c for c in m15 if c.ts >= break_ts]
    since_tap = [c for c in m15 if c.ts >= tap_ts]
    if side is Side.SELL:
        highs = [
            since_break[i].high
            for i in range(len(since_break) - 1)
            if since_break[i].bullish and since_break[i + 1].bearish
        ]
        if highs:
            return max(highs)
        return max((c.high for c in since_tap), default=None)
    lows = [
        since_break[i].low
        for i in range(len(since_break) - 1)
        if since_break[i].bearish and since_break[i + 1].bullish
    ]
    if lows:
        return min(lows)
    return min((c.low for c in since_tap), default=None)


class SetupMachine:
    def __init__(
        self,
        symbol: str,
        direction: str | None = None,
        state: SetupState = SetupState.IDLE,
        pending: PendingSetup | None = None,
    ) -> None:
        self.symbol = symbol
        self.direction = direction
        self.state = state if direction else SetupState.IDLE
        self.pending = pending

    # -------------------------------------------------------------- helpers
    @property
    def retracement_side(self) -> Side | None:
        if self.direction == "BEARISH":
            return Side.BUY
        if self.direction == "BULLISH":
            return Side.SELL
        return None

    @property
    def opposite_role(self) -> str | None:
        """Role of the Daily SNR the retracement starts from."""
        if self.direction == "BEARISH":
            return "S"
        if self.direction == "BULLISH":
            return "R"
        return None

    def _invalidate(self, reason: str) -> list[SetupEvent]:
        self.pending = None
        self.state = SetupState.DIRECTION_SET if self.direction else SetupState.IDLE
        return [SetupEvent("INVALIDATED", {"reason": reason})]

    # ------------------------------------------------------------ direction
    def set_direction(self, direction: str | None) -> list[SetupEvent]:
        """Direction change from the DirectionEngine. A flip mid-setup is
        invalidation §6.8-5."""
        events: list[SetupEvent] = []
        if direction == self.direction:
            return events
        if self.pending is not None:
            events += self._invalidate("direction_flip")
        self.direction = direction
        self.state = SetupState.DIRECTION_SET if direction else SetupState.IDLE
        events.append(SetupEvent("STATE", {"state": self.state.value}))
        return events

    # ------------------------------------------------------------------ M15
    def on_m15_close(
        self,
        candle: Candle,
        m15_history: list[Candle],
        fresh_opposite_levels: list[SNRLevel],
    ) -> list[SetupEvent]:
        """Tap detection, then the wick-to-body structural break (§6.6-4A)."""
        side = self.retracement_side
        if side is None:
            return []

        if self.pending is None:
            # AWAIT_TAP: watching every fresh opposite Daily SNR.
            if self.state == SetupState.DIRECTION_SET and fresh_opposite_levels:
                self.state = SetupState.AWAIT_TAP
            for lvl in fresh_opposite_levels:
                if not (lvl.fresh and lvl.active and not lvl.played):
                    continue
                if candle.low <= lvl.price <= candle.high:
                    self.pending = PendingSetup(
                        side=side,
                        level_id=lvl.id,
                        level_price=lvl.price,
                        level_role=lvl.role,
                        tap_ts=candle.ts,
                    )
                    # Rejection is validated continuously from here on.
                    self.state = SetupState.M15_BREAK_PENDING
                    return [
                        SetupEvent("TAPPED", {"price": lvl.price, "side": side.value}),
                        SetupEvent("STATE", {"state": SetupState.REJECTION_PENDING.value}),
                        SetupEvent("STATE", {"state": self.state.value}),
                    ]
            return []

        if self.state == SetupState.M15_BREAK_PENDING:
            pend = self.pending
            # Swing to break: last M15 swing BEFORE the tap (its confirming
            # candle may be later). Recompute until the break lands.
            history_to_now = [c for c in m15_history if c.ts <= candle.ts]
            eligible = [
                (i, c) for i, c in enumerate(history_to_now) if c.ts <= pend.tap_ts
            ]
            swing = None
            if eligible:
                if pend.side is Side.BUY:
                    for i, c in reversed(eligible):
                        if c.bullish and i + 1 < len(history_to_now) and history_to_now[i + 1].bearish:
                            swing = ("HIGH", c.high, c)
                            break
                else:
                    for i, c in reversed(eligible):
                        if c.bearish and i + 1 < len(history_to_now) and history_to_now[i + 1].bullish:
                            swing = ("LOW", c.low, c)
                            break
            if swing is None:
                return []
            kind, price, swing_candle = swing
            pend.swing_price = price
            pend.swing_candle = _candle_dict(swing_candle)

            broke = candle.close > price if pend.side is Side.BUY else candle.close < price
            if broke:
                pend.break_ts = candle.ts
                pend.order_block = {
                    "high": swing_candle.high,
                    "low": swing_candle.low,
                    "ts": swing_candle.ts.isoformat(),
                }
                self.state = SetupState.H1_ENGULF_PENDING
                return [
                    SetupEvent("M15_BREAK", {"swing": price, "close": candle.close}),
                    SetupEvent("STATE", {"state": self.state.value}),
                ]
        return []

    # ------------------------------------------------------------------ H1
    def on_h1_close(
        self, candle: Candle, h1_history: list[Candle], m15_history: list[Candle]
    ) -> list[SetupEvent]:
        if self.pending is None or self.state != SetupState.H1_ENGULF_PENDING:
            return []
        pend = self.pending

        # §6.8-3: H1 must NOT break structure — checked before engulfing.
        prior = [c for c in h1_history if c.ts < candle.ts]
        h1_swing = last_swing_high(prior) if pend.side is Side.BUY else last_swing_low(prior)
        if h1_swing is not None and m15_wick_to_body_break(candle, h1_swing):
            return self._invalidate("h1_structure_break")

        direction = "BULLISH" if pend.side is Side.BUY else "BEARISH"
        upto = [c for c in h1_history if c.ts <= candle.ts]
        engulf = detect_engulfing(upto, direction)
        if engulf is None or pend.break_ts is None or engulf.final.ts < pend.break_ts:
            return []

        ob = pend.order_block or {}
        entry = ob.get("high") if pend.side is Side.BUY else ob.get("low")
        strong = compute_strong_extreme(m15_history, pend.side, pend.break_ts, pend.tap_ts)
        if entry is None or strong is None:
            return self._invalidate("degenerate_plan")
        try:
            plan = build_plan(pend.side, float(entry), float(strong))
        except ValueError:
            return self._invalidate("degenerate_plan")

        self.state = SetupState.SETUP_CONFIRMED
        confirmed = SetupEvent(
            "CONFIRMED",
            {
                "plan": plan,
                "order_block": ob,
                "level_id": pend.level_id,
                "level_price": pend.level_price,
                "engulf_type": engulf.type,
                "tap_ts": pend.tap_ts.isoformat(),
            },
        )
        # The machine's job ends here; signal lifecycle is external. Reset for
        # the next hunt (the played level is defreshed by the caller).
        self.pending = None
        self.state = SetupState.DIRECTION_SET
        return [confirmed, SetupEvent("STATE", {"state": self.state.value})]

    # ------------------------------------------------------------------ H4/D1
    def on_h4_close(self, candle: Candle) -> list[SetupEvent]:
        """§6.8-1 (and the 4B timing rule): H4 body close beyond the level."""
        if self.pending is None:
            return []
        if _closes_beyond(candle.close, self.pending.level_price, self.pending.level_role):
            return self._invalidate("h4_close_beyond")
        return []

    def on_d1_close(self, candle: Candle) -> list[SetupEvent]:
        """§6.8-2: Daily body close beyond the level."""
        if self.pending is None:
            return []
        if _closes_beyond(candle.close, self.pending.level_price, self.pending.level_role):
            return self._invalidate("d1_close_beyond")
        return []
