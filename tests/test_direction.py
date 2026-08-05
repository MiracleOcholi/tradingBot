"""Direction engine: tap + H4 body-to-body break, validity, mirror flip."""
from datetime import datetime, timezone

from app.core.direction import DirectionEngine
from app.core.models import Formation, SNRLevel
from tests.helpers import T0, bear, bull, mk


def d1_level(price: float, role: str, fresh: bool = True, lid: str = "L1") -> SNRLevel:
    return SNRLevel(
        symbol="R_10", timeframe="D1", price=price,
        formation=Formation.TRAD_S if role == "S" else Formation.TRAD_R,
        role=role, first_candle_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        fresh=fresh, id=lid,
    )


def h4_trad(price: float, role: str) -> SNRLevel:
    return SNRLevel(
        symbol="R_10", timeframe="H4", price=price,
        formation=Formation.TRAD_R if role == "R" else Formation.TRAD_S,
        role=role, first_candle_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )


def test_bullish_confirmation_tap_then_h4_break():
    eng = DirectionEngine("R_10")
    support = d1_level(100.0, "S")
    ref = h4_trad(104.0, "R")

    evs = eng.on_m15_close(mk(0, 101, 102, 99.9, 101.5), [support])  # taps 100
    assert [e.kind for e in evs] == ["TAP"]
    assert eng.state.candidate.side == "BULLISH"

    # H4 closes below ref → no break yet
    assert eng.on_h4_close(bull(1, 101, 103), lambda r, b: ref) == []
    # H4 body closes above the last Traditional R → confirmed
    evs = eng.on_h4_close(bull(2, 103, 104.5), lambda r, b: ref)
    assert [e.kind for e in evs] == ["CONFIRMED"]
    assert eng.state.direction == "BULLISH"
    assert eng.state.candidate is None


def test_tap_requires_fresh_level_and_exact_touch():
    eng = DirectionEngine("R_10")
    tested = d1_level(100.0, "S", fresh=False)
    assert eng.on_m15_close(mk(0, 101, 102, 99.9, 101.5), [tested]) == []
    fresh = d1_level(100.0, "S")
    no_touch = mk(1, 101, 102, 100.4, 101.5)     # low never reaches 100
    assert eng.on_m15_close(no_touch, [fresh]) == []


def test_h4_close_beyond_level_kills_candidate():
    eng = DirectionEngine("R_10")
    support = d1_level(100.0, "S")
    eng.on_m15_close(mk(0, 101, 102, 99.9, 101.5), [support])
    evs = eng.on_h4_close(bear(1, 101, 99.0), lambda r, b: h4_trad(104.0, "R"))
    assert [e.kind for e in evs] == ["REJECT_INVALID"]
    assert eng.state.candidate is None and eng.state.direction is None


def test_d1_close_beyond_level_kills_candidate():
    eng = DirectionEngine("R_10")
    resistance = d1_level(110.0, "R")
    eng.on_m15_close(mk(0, 109, 110.2, 108, 109.5), [resistance])
    evs = eng.on_d1_close(bull(1, 109, 111))      # daily body close above R
    assert [e.kind for e in evs] == ["REJECT_INVALID"]


def test_mirror_event_flips_direction():
    eng = DirectionEngine("R_10")
    eng.state.direction = "BULLISH"
    resistance = d1_level(110.0, "R", lid="L2")
    eng.on_m15_close(mk(0, 109, 110.2, 108, 109.5), [resistance])
    assert eng.state.candidate.side == "BEARISH"
    evs = eng.on_h4_close(bear(1, 109, 105.0), lambda r, b: h4_trad(106.0, "S"))
    assert [e.kind for e in evs] == ["FLIPPED"]
    assert eng.state.direction == "BEARISH"


def test_same_side_tap_ignored_when_direction_already_set():
    eng = DirectionEngine("R_10")
    eng.state.direction = "BULLISH"
    support = d1_level(100.0, "S")               # would confirm BULLISH again
    assert eng.on_m15_close(mk(0, 101, 102, 99.9, 101.5), [support]) == []


def test_no_reference_level_no_confirmation():
    eng = DirectionEngine("R_10")
    eng.on_m15_close(mk(0, 101, 102, 99.9, 101.5), [d1_level(100.0, "S")])
    assert eng.on_h4_close(bull(1, 101, 105), lambda r, b: None) == []
    assert eng.state.direction is None
