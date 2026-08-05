"""SNR engine tests: detection, marking rule, freshness, SBR/RBS/Left Shoulder."""
from app.core.models import Formation
from app.core.snr import SNRTracker, detect_new_snr
from tests.helpers import T0, bear, bull, mk


def make_tracker(**kw):
    return SNRTracker("R_10", "H4", 900, **kw)  # 900s grid to match helpers


# ---------------------------------------------------------------- detection
def test_traditional_resistance_bull_then_bear():
    assert detect_new_snr(bull(0, 100, 105), bear(1, 105, 101)) is Formation.TRAD_R


def test_traditional_support_bear_then_bull():
    assert detect_new_snr(bear(0, 105, 100), bull(1, 100, 104)) is Formation.TRAD_S


def test_open_close_resistance_bear_bear():
    assert detect_new_snr(bear(0, 105, 100), bear(1, 100, 96)) is Formation.OC_R


def test_open_close_support_bull_bull():
    assert detect_new_snr(bull(0, 100, 104), bull(1, 104, 108)) is Formation.OC_S


def test_doji_forms_nothing():
    doji = mk(0, 100, 101, 99, 100)
    assert detect_new_snr(doji, bull(1, 100, 104)) is None
    assert detect_new_snr(bull(0, 100, 104), mk(1, 104, 105, 103, 104)) is None


# ---------------------------------------------------------------- marking
def test_level_exists_only_after_second_close_and_marks_first_close():
    tr = make_tracker()
    upd0 = tr.process_candle(bull(0, 100, 105))
    assert upd0.new_level is None            # one candle → no SNR yet
    upd1 = tr.process_candle(bear(1, 105, 101))
    lvl = upd1.new_level
    assert lvl is not None
    assert lvl.formation is Formation.TRAD_R
    assert lvl.price == 105                  # close of the FIRST candle
    assert lvl.first_candle_at == T0
    assert lvl.role == "R"


def test_marking_point_never_moves_after_flip():
    tr = make_tracker()
    tr.process_candle(bull(0, 100, 105))
    lvl = tr.process_candle(bear(1, 105, 101)).new_level
    tr.process_candle(bull(2, 101, 107))     # body close above 105 → RBS
    assert lvl.role == "S"
    assert lvl.price == 105                  # marked price unchanged


# ---------------------------------------------------------------- freshness
def test_fresh_until_first_touch_then_tested():
    tr = make_tracker()
    tr.process_candle(bull(0, 100, 105))
    lvl = tr.process_candle(bear(1, 105, 101)).new_level   # R @ 105
    assert lvl.fresh and lvl.touches == 0

    tr.process_candle(mk(3, 101, 103, 100, 102))           # stays below: no touch
    assert lvl.fresh and lvl.touches == 0

    upd = tr.process_candle(mk(4, 102, 105.5, 101, 103))   # wick reaches 105 → touch
    assert lvl in upd.touched
    assert lvl.touches == 1 and not lvl.fresh


def test_forming_candles_do_not_count_as_touches():
    tr = make_tracker()
    tr.process_candle(bull(0, 100, 105))
    lvl = tr.process_candle(bear(1, 105, 101)).new_level
    # The 2nd forming candle opened AT 105 but must not count as a touch.
    assert lvl.touches == 0 and lvl.fresh


# ---------------------------------------------------------------- role flips
def test_sbr_support_breaks_to_resistance_on_body_close_only():
    tr = make_tracker()
    tr.process_candle(bear(0, 105, 100))
    lvl = tr.process_candle(bull(1, 100, 104)).new_level   # S @ 100
    assert lvl.role == "S"

    # Wick below 100 but body closes above → NO flip (touch only).
    upd = tr.process_candle(mk(3, 101, 102, 99.5, 100.5))
    assert lvl not in upd.flipped and lvl.role == "S"

    # Body close below 100 → SBR.
    upd = tr.process_candle(bear(4, 100.5, 99))
    assert lvl in upd.flipped
    assert lvl.role == "R" and lvl.break_count == 1


def test_rbs_resistance_breaks_to_support():
    tr = make_tracker()
    tr.process_candle(bull(0, 100, 105))
    lvl = tr.process_candle(bear(1, 105, 101)).new_level   # R @ 105
    tr.process_candle(bull(3, 101, 106))                   # body close above → RBS
    assert lvl.role == "S" and lvl.break_count == 1


def test_left_shoulder_requires_two_breaks():
    tr = make_tracker()
    tr.process_candle(bear(0, 105, 100))
    lvl = tr.process_candle(bull(1, 100, 104)).new_level   # S @ 100
    tr.process_candle(bear(3, 100.5, 99))                  # break 1 → SBR (now R)
    assert lvl.break_count == 1 and lvl.role == "R"
    tr.process_candle(bull(4, 99, 101))                    # break 2 → Left Shoulder
    assert lvl.break_count == 2 and lvl.role == "S"


def test_break_destroys_freshness_even_without_wick_touch():
    tr = make_tracker()
    tr.process_candle(bull(0, 100, 105))
    lvl = tr.process_candle(bear(1, 105, 101)).new_level   # R @ 105, fresh
    tr.process_candle(bull(3, 101, 107))                   # breaks straight through
    assert not lvl.fresh and lvl.touches >= 1


def test_open_close_levels_flip_too():
    tr = make_tracker()
    tr.process_candle(bear(0, 105, 100))
    lvl = tr.process_candle(bear(1, 100, 96)).new_level    # OC_R @ 100
    assert lvl.formation is Formation.OC_R and lvl.role == "R"
    tr.process_candle(bull(3, 96, 101))                    # close above → RBS
    assert lvl.role == "S" and lvl.break_count == 1


# ---------------------------------------------------------------- helpers
def test_last_traditional_ignores_open_close_and_respects_before():
    tr = make_tracker()
    tr.process_candle(bear(0, 105, 100))
    tr.process_candle(bear(1, 100, 96))       # OC_R @ 100 (candle0.close)
    tr.process_candle(bull(2, 96, 99))        # TRAD_S @ 96 formed (bear1→bull2)
    tr.process_candle(bear(3, 99, 97))        # TRAD_R @ 99 formed (bull2→bear3)
    last_r = tr.last_traditional("R")
    assert last_r is not None and last_r.formation is Formation.TRAD_R
    assert last_r.price == 99                 # not the OC_R @ 100
    # `before` excludes levels formed at/after the cutoff
    early = tr.last_traditional("R", before=T0)
    assert early is None


def test_max_active_deactivates_oldest():
    tr = SNRTracker("R_10", "H4", 900, max_active=3)
    prices = [(100, 104), (104, 108), (108, 112), (112, 116), (116, 120)]
    for i, (lo, hi) in enumerate(prices):
        tr.process_candle(bull(i, lo, hi))    # consecutive bull→bull = OC_S each pair
    assert len(tr.active_levels()) <= 3
