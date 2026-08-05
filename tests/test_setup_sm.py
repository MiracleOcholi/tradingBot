"""Setup state machine: full path §8.3 + all five invalidations §6.8."""
from datetime import datetime, timezone

import pytest

from app.core.models import Formation, SetupState, Side, SNRLevel
from app.core.setup_sm import SetupMachine, compute_strong_extreme
from tests.helpers import T0, bear, bull, mk


def daily_support(price: float = 100.0, lid: str = "D1S") -> SNRLevel:
    return SNRLevel(
        symbol="R_10", timeframe="D1", price=price, formation=Formation.TRAD_S,
        role="S", first_candle_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        fresh=True, id=lid,
    )


def daily_resistance(price: float = 120.0, lid: str = "D1R") -> SNRLevel:
    return SNRLevel(
        symbol="R_10", timeframe="D1", price=price, formation=Formation.TRAD_R,
        role="R", first_candle_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        fresh=True, id=lid,
    )


def bearish_machine() -> SetupMachine:
    """Direction BEARISH → hunting a BULLISH retracement (BUY at fresh support)."""
    sm = SetupMachine("R_10")
    sm.set_direction("BEARISH")
    return sm


def drive_to_break(sm: SetupMachine, level: SNRLevel):
    """Common prologue: sell-off → swing high → tap of support → M15 break up.

    Returns (m15_history, break_candle).
    """
    m15 = [
        bear(0, 106, 104),          # falling
        bull(1, 104, 104.8),        # pullback top → swing HIGH candle (h=105.0)
        bear(2, 104.8, 102),        # bearish confirm → swing high confirmed
        bear(3, 102, 101),
    ]
    for c in m15:
        assert sm.on_m15_close(c, m15, [level]) == []
    tap = mk(4, 101, 101.5, 99.9, 100.8)          # touches 100 exactly
    m15.append(tap)
    evs = sm.on_m15_close(tap, m15, [level])
    assert [e.kind for e in evs][0] == "TAPPED"
    assert sm.state == SetupState.M15_BREAK_PENDING

    brk = bull(5, 100.8, 105.4)                   # body close 105.4 > wick 105.0
    m15.append(brk)
    evs = sm.on_m15_close(brk, m15, [level])
    assert [e.kind for e in evs][0] == "M15_BREAK"
    assert sm.state == SetupState.H1_ENGULF_PENDING
    return m15, brk


# ---------------------------------------------------------------- happy path
def test_full_bullish_retracement_confirms_with_correct_plan():
    sm = bearish_machine()
    level = daily_support()
    m15, _ = drive_to_break(sm, level)

    # H1: bearish candle then a Type-1 bullish engulfing after the break.
    h1_prev = bear(0, 103, 101, step_s=3600)
    h1_engulf = bull(6, 101, 103.5, step_s=900)   # ts after break; closes above 103.2 wick
    h1 = [h1_prev, h1_engulf]
    evs = sm.on_h1_close(h1_engulf, h1, m15)
    kinds = [e.kind for e in evs]
    assert "CONFIRMED" in kinds
    data = next(e.data for e in evs if e.kind == "CONFIRMED")
    plan = data["plan"]
    assert plan.side is Side.BUY
    # Entry = proximal edge of the order block = swing candle's wick HIGH (105.0)
    assert plan.entry == pytest.approx(105.0)
    # SL = strong low since tap (99.9 tap wick) — no post-break swing low yet
    assert plan.sl == pytest.approx(99.9)
    assert plan.tp == pytest.approx(plan.entry + 4 * (plan.entry - plan.sl))
    # machine resets for the next hunt, direction retained
    assert sm.state == SetupState.DIRECTION_SET and sm.pending is None


def test_tap_requires_exact_line_touch():
    sm = bearish_machine()
    level = daily_support()
    near_miss = mk(0, 101, 102, 100.05, 101.2)    # low stops 0.05 above the line
    assert sm.on_m15_close(near_miss, [near_miss], [level]) == []
    assert sm.state in (SetupState.DIRECTION_SET, SetupState.AWAIT_TAP)


def test_played_or_tested_levels_are_ignored():
    sm = bearish_machine()
    tested = daily_support()
    tested.fresh = False
    tap = mk(0, 101, 101.5, 99.9, 100.8)
    assert sm.on_m15_close(tap, [tap], [tested]) == []
    played = daily_support(lid="D1S2")
    played.played = True
    assert sm.on_m15_close(tap, [tap], [played]) == []


def test_rule_of_opposites_needs_direction():
    sm = SetupMachine("R_10")                     # no direction
    level = daily_support()
    tap = mk(0, 101, 101.5, 99.9, 100.8)
    assert sm.on_m15_close(tap, [tap], [level]) == []
    assert sm.state == SetupState.IDLE


# ---------------------------------------------------------------- invalidations
def test_inv1_h4_close_beyond_level():
    sm = bearish_machine()
    level = daily_support()
    drive_to_break(sm, level)
    evs = sm.on_h4_close(bear(9, 101, 99.0, step_s=14400))   # body close below 100
    assert [e.kind for e in evs] == ["INVALIDATED"]
    assert sm.state == SetupState.DIRECTION_SET and sm.pending is None


def test_inv2_d1_close_beyond_level():
    sm = bearish_machine()
    level = daily_support()
    drive_to_break(sm, level)
    evs = sm.on_d1_close(bear(1, 101, 99.5, step_s=86400))
    assert [e.kind for e in evs] == ["INVALIDATED"]


def test_inv3_h1_structure_break_beats_engulfing():
    sm = bearish_machine()
    level = daily_support()
    m15, _ = drive_to_break(sm, level)
    # H1 history: swing high at 104.2 confirmed by a bear, then a candle that
    # both engulfs AND body-closes above the H1 swing wick → structure break.
    h1 = [
        bull(0, 102, 104, step_s=3600),           # high 104.2
        bear(1, 104, 101, step_s=3600),           # confirms swing high
        bull(6, 101, 105.0, step_s=900),          # closes above 104.2 swing wick
    ]
    evs = sm.on_h1_close(h1[-1], h1, m15)
    assert [e.kind for e in evs] == ["INVALIDATED"]
    assert evs[0].data["reason"] == "h1_structure_break"


def test_inv4_h4_close_beyond_before_m15_break():
    sm = bearish_machine()
    level = daily_support()
    m15 = [bear(0, 106, 104), bull(1, 104, 104.8), bear(2, 104.8, 102)]
    tap = mk(3, 102, 102.5, 99.9, 100.8)
    m15.append(tap)
    sm.on_m15_close(tap, m15, [level])
    assert sm.state == SetupState.M15_BREAK_PENDING     # break not yet posted
    evs = sm.on_h4_close(bear(1, 101, 99.2, step_s=14400))
    assert [e.kind for e in evs] == ["INVALIDATED"]
    assert evs[0].data["reason"] == "h4_close_beyond"


def test_inv5_direction_flip_mid_setup():
    sm = bearish_machine()
    level = daily_support()
    drive_to_break(sm, level)
    evs = sm.set_direction("BULLISH")
    assert [e.kind for e in evs][0] == "INVALIDATED"
    assert evs[0].data["reason"] == "direction_flip"
    assert sm.state == SetupState.DIRECTION_SET         # new direction armed


# ---------------------------------------------------------------- details
def test_engulfing_before_m15_break_does_not_confirm():
    sm = bearish_machine()
    level = daily_support()
    m15, brk = drive_to_break(sm, level)
    stale_prev = bear(0, 103, 101, step_s=3600)
    stale_engulf = bull(2, 101, 103.5, step_s=900)      # ts BEFORE the break ts
    assert sm.on_h1_close(stale_engulf, [stale_prev, stale_engulf], m15) == []
    assert sm.state == SetupState.H1_ENGULF_PENDING


def test_bearish_retracement_mirror_confirms_sell():
    sm = SetupMachine("R_10")
    sm.set_direction("BULLISH")                          # hunt SELL at fresh R
    level = daily_resistance(120.0)
    m15 = [
        bull(0, 114, 116),
        bear(1, 116, 115.2),        # pullback bottom → swing LOW candle (l=115.0)
        bull(2, 115.2, 118),        # bullish confirm → swing low confirmed
        bull(3, 118, 119),
    ]
    for c in m15:
        assert sm.on_m15_close(c, m15, [level]) == []
    tap = mk(4, 119, 120.1, 118.5, 119.3)                # touches 120
    m15.append(tap)
    evs = sm.on_m15_close(tap, m15, [level])
    assert [e.kind for e in evs][0] == "TAPPED"

    brk = bear(5, 119.3, 114.6)                          # closes below wick 114.8
    m15.append(brk)
    evs = sm.on_m15_close(brk, m15, [level])
    assert [e.kind for e in evs][0] == "M15_BREAK"

    h1 = [bull(0, 116, 118, step_s=3600), bear(6, 118, 115.6, step_s=900)]
    evs = sm.on_h1_close(h1[-1], h1, m15)
    data = next(e.data for e in evs if e.kind == "CONFIRMED")
    plan = data["plan"]
    assert plan.side is Side.SELL
    assert plan.entry == pytest.approx(115.0)            # OB proximal edge = zone low
    assert plan.sl == pytest.approx(120.1)               # strong high = tap wick
    assert plan.tp == pytest.approx(plan.entry - 4 * (plan.sl - plan.entry))


def test_strong_extreme_prefers_confirmed_post_break_swing():
    # SELL: post-break pullback makes a confirmed swing high below the tap peak.
    m15 = [
        mk(0, 119, 120.1, 118, 119.3),   # tap candle (peak 120.1)
        bear(1, 119.3, 114.6),           # break candle
        bull(2, 114.6, 116),             # pullback high 116.2
        bear(3, 116, 114),               # confirms the swing high
    ]
    strong = compute_strong_extreme(m15, Side.SELL, break_ts=m15[1].ts, tap_ts=m15[0].ts)
    assert strong == pytest.approx(116.2)                # NOT the 120.1 peak
