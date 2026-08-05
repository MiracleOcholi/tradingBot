"""Entry / SL / TP math, including the Telegram Edit-one-value recompute.

Rules (PDF §6.9, §8.4 + HANDOFF-V2):
- Entry  = proximal edge of the M15 order block (first touch).
- SL     = just beyond the post-M15-break strong high/low.
- TP     = fixed 1:4 from entry and stop, on the profit side.
- Edit changes ONE value; the others are recomputed from the 1:4 ratio and
  the stop relationship (SL and TP on opposite sides of entry).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from app.core.models import Side

RR = 4.0  # fixed 1:4 risk-to-reward


@dataclass(frozen=True)
class TradePlan:
    side: Side
    entry: float
    sl: float
    tp: float

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def reward(self) -> float:
        return abs(self.tp - self.entry)


def validate(plan: TradePlan) -> None:
    """Raise ValueError unless SL/TP sit on the correct sides of entry."""
    if plan.risk <= 0:
        raise ValueError("SL must differ from entry")
    if plan.side is Side.BUY:
        if not (plan.sl < plan.entry < plan.tp):
            raise ValueError("BUY requires SL < entry < TP")
    else:
        if not (plan.tp < plan.entry < plan.sl):
            raise ValueError("SELL requires TP < entry < SL")


def build_plan(side: Side, entry: float, sl: float) -> TradePlan:
    """Derive TP at fixed 1:4 from a structural entry and stop."""
    risk = abs(entry - sl)
    tp = entry + RR * risk if side is Side.BUY else entry - RR * risk
    plan = TradePlan(side=side, entry=entry, sl=sl, tp=tp)
    validate(plan)
    return plan


def recompute(plan: TradePlan, field: str, value: float) -> TradePlan:
    """Apply an Edit to exactly one field and recompute the rest (1:4 held).

    - edit entry → SL keeps its structural price; TP recomputed at 1:4.
    - edit sl    → entry unchanged; TP recomputed at 1:4.
    - edit tp    → entry unchanged; SL recomputed so |tp-entry| = 4·|entry-sl|.
    """
    if field == "entry":
        return build_plan(plan.side, value, plan.sl)
    if field == "sl":
        return build_plan(plan.side, plan.entry, value)
    if field == "tp":
        reward = value - plan.entry if plan.side is Side.BUY else plan.entry - value
        if reward <= 0:
            raise ValueError("TP must sit on the profit side of entry")
        risk = reward / RR
        sl = plan.entry - risk if plan.side is Side.BUY else plan.entry + risk
        new = TradePlan(side=plan.side, entry=plan.entry, sl=sl, tp=value)
        validate(new)
        return new
    raise ValueError(f"unknown field: {field}")
