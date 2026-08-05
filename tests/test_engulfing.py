"""H1 engulfing detection: Type 1, Type 2, body-close rule, mixed-colour ban."""
from app.core.engulfing import detect_engulfing
from tests.helpers import bear, bull, mk


def test_type1_bullish_engulfing():
    prev = bear(0, 105, 101)                       # engulfed candle, high 105.2
    final = bull(1, 101, 105.5)                    # body close above full wick
    e = detect_engulfing([prev, final], "BULLISH")
    assert e is not None
    assert e.type == 1 and e.run_length == 1
    assert e.engulfed is prev                      # zone = engulfed candle


def test_wick_alone_never_confirms():
    prev = bear(0, 105, 101)
    probe = mk(1, 101, 106, 100.5, 104)            # wick above 105.2, body below
    assert detect_engulfing([prev, probe], "BULLISH") is None


def test_type2_two_same_colour_candles():
    prev = bear(0, 105, 101)
    c1 = bull(1, 101, 103)                         # doesn't clear the wick yet
    c2 = bull(2, 103, 105.6)                       # final closes above 105.2
    e = detect_engulfing([prev, c1, c2], "BULLISH")
    assert e is not None
    assert e.type == 2 and e.run_length == 2
    assert e.engulfed is prev


def test_type2_mixed_colours_invalid():
    prev = bear(0, 105, 101)
    c1 = bull(1, 101, 103)
    c2 = bear(2, 103, 102)                         # colour breaks the run
    c3 = bull(3, 102, 105.6)
    # run walk-back stops at c2 (bearish): c3 alone must engulf c2 — and the
    # "engulfed" candle it finds (c2) is bearish, so this is Type 1 on c2,
    # NOT a Type 2 on prev. Verify prev's wick is irrelevant now:
    e = detect_engulfing([prev, c1, c2, c3], "BULLISH")
    assert e is not None and e.engulfed is c2 and e.type == 1


def test_earlier_completion_in_run_means_not_confirmed_now():
    prev = bear(0, 105, 101)
    c1 = bull(1, 101, 105.4)                       # already closed above the wick
    c2 = bull(2, 105.4, 106)
    # the engulf confirmed at c1's close; c2 must not re-report it
    assert detect_engulfing([prev, c1, c2], "BULLISH") is None


def test_bearish_mirror_type1_and_type2():
    prev = bull(0, 100, 104)                       # engulfed, low 99.8
    t1 = detect_engulfing([prev, bear(1, 104, 99.5)], "BEARISH")
    assert t1 is not None and t1.type == 1

    c1 = bear(1, 104, 101)
    c2 = bear(2, 101, 99.5)
    t2 = detect_engulfing([prev, c1, c2], "BEARISH")
    assert t2 is not None and t2.type == 2 and t2.engulfed is prev


def test_engulfed_must_be_opposite_colour():
    prev = bull(0, 100, 104)                       # bullish before a bullish final
    final = bull(1, 104, 108)
    assert detect_engulfing([prev, final], "BULLISH") is None


def test_direction_of_final_candle_must_match():
    prev = bear(0, 105, 101)
    final = bear(1, 101, 100)                      # bearish can't be a BULLISH engulf
    assert detect_engulfing([prev, final], "BULLISH") is None
