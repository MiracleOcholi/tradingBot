"""Swings + the two break types (H4 body-to-body, M15 wick-to-body)."""
from app.core.breaks import (
    h4_body_to_body_break,
    last_swing_high,
    last_swing_low,
    m15_wick_to_body_break,
)
from tests.helpers import bear, bull, mk


# ---------------------------------------------------------------- swings
def test_swing_high_needs_one_bearish_confirmation():
    candles = [bull(0, 100, 104), bull(1, 104, 108), bear(2, 108, 105)]
    sw = last_swing_high(candles)
    assert sw is not None
    assert sw.price == candles[1].high       # extreme candle before the bear
    assert sw.confirmed_by is candles[2]


def test_swing_low_needs_one_bullish_confirmation():
    candles = [bear(0, 110, 106), bear(1, 106, 102), bull(2, 102, 105)]
    sw = last_swing_low(candles)
    assert sw is not None
    assert sw.price == candles[1].low


def test_no_swing_without_opposing_candle():
    only_bulls = [bull(i, 100 + i, 101 + i) for i in range(4)]
    assert last_swing_high(only_bulls) is None   # never followed by a bear


def test_single_opposing_candle_inside_a_run_marks_pivot():
    # bearish run, one bull, run resumes — that bull confirms the swing low
    candles = [bear(0, 110, 107), bear(1, 107, 104), bull(2, 104, 105.5), bear(3, 105.5, 103)]
    sw = last_swing_low(candles)
    assert sw.price == candles[1].low
    assert sw.confirmed_by is candles[2]


# ---------------------------------------------------------------- H4 body-to-body
def test_h4_break_is_body_close_beyond_marked_price():
    level = 105.0
    wick_only = mk(0, 103, 106, 102, 104.5)   # wick above, body below
    assert not h4_body_to_body_break(wick_only, level, "BULLISH")
    body_close = bull(1, 104, 105.5)
    assert h4_body_to_body_break(body_close, level, "BULLISH")


def test_h4_break_bearish_mirror():
    level = 100.0
    assert h4_body_to_body_break(bear(0, 101, 99.5), level, "BEARISH")
    assert not h4_body_to_body_break(mk(1, 101, 102, 99.2, 100.4), level, "BEARISH")


# ---------------------------------------------------------------- M15 wick-to-body
def test_m15_break_needs_body_close_beyond_swing_wick():
    candles = [bull(0, 100, 104), bull(1, 104, 108), bear(2, 108, 105)]
    sw = last_swing_high(candles)             # wick extreme 108.2
    wick_probe = mk(3, 105, 108.4, 104, 107)  # wick pierces, body below
    assert not m15_wick_to_body_break(wick_probe, sw)
    body_break = bull(4, 107, 108.5)          # body closes above the wick
    assert m15_wick_to_body_break(body_break, sw)


def test_m15_bearish_break_of_swing_low():
    candles = [bear(0, 110, 106), bear(1, 106, 102), bull(2, 102, 105)]
    sw = last_swing_low(candles)              # wick extreme 101.8
    assert m15_wick_to_body_break(bear(3, 104, 101.5), sw)
    assert not m15_wick_to_body_break(mk(4, 103, 104, 101.5, 102.5), sw)
