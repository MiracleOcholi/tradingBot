"""Candle-completion logic of the Deriv stream (pure, no network)."""
from app.services.deriv import GRANULARITY, TF_OF_GRANULARITY, CandleStream


def _ohlc(open_time: int, o, h, l, c):
    return {"open_time": open_time, "open": o, "high": h, "low": l, "close": c,
            "symbol": "R_10", "granularity": 900}


def test_history_ingest_marks_last_as_forming():
    s = CandleStream("R_10", "M15")
    done = s.ingest_history([
        {"epoch": 0, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
        {"epoch": 900, "open": 1.5, "high": 2.5, "low": 1, "close": 2},
        {"epoch": 1800, "open": 2, "high": 2.2, "low": 1.8, "close": 2.1},
    ])
    assert len(done) == 2                     # last one is still forming
    assert s.forming is not None and int(s.forming.ts.timestamp()) == 1800


def test_ohlc_updates_same_candle_do_not_complete():
    s = CandleStream("R_10", "M15")
    s.ingest_history([{"epoch": 0, "open": 1, "high": 1, "low": 1, "close": 1}])
    assert s.ingest_ohlc(_ohlc(0, 1, 1.2, 0.9, 1.1)) is None
    assert s.ingest_ohlc(_ohlc(0, 1, 1.3, 0.9, 1.2)) is None
    assert s.forming.close == 1.2


def test_new_open_time_completes_previous_candle():
    s = CandleStream("R_10", "M15")
    s.ingest_history([{"epoch": 0, "open": 1, "high": 1, "low": 1, "close": 1}])
    s.ingest_ohlc(_ohlc(0, 1, 1.3, 0.9, 1.25))
    finished = s.ingest_ohlc(_ohlc(900, 1.25, 1.26, 1.24, 1.25))
    assert finished is not None
    assert finished.close == 1.25 and int(finished.ts.timestamp()) == 0
    assert int(s.forming.ts.timestamp()) == 900


def test_granularity_maps_are_inverse():
    for tf, g in GRANULARITY.items():
        assert TF_OF_GRANULARITY[g] == tf
