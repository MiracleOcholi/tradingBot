"""Synthetic-candle helpers for engine tests."""
from datetime import UTC, datetime, timedelta

from app.core.models import Candle

T0 = datetime(2026, 8, 1, tzinfo=UTC)


def mk(i: int, o: float, h: float, l: float, c: float, step_s: int = 900) -> Candle:
    """Candle #i on a regular grid (default M15)."""
    return Candle(ts=T0 + timedelta(seconds=i * step_s), open=o, high=h, low=l, close=c)


def bull(i: int, lo: float, hi: float, step_s: int = 900) -> Candle:
    """Bullish candle: opens at lo, closes at hi, small wicks."""
    return mk(i, lo, hi + 0.2, lo - 0.2, hi, step_s)


def bear(i: int, hi: float, lo: float, step_s: int = 900) -> Candle:
    """Bearish candle: opens at hi, closes at lo, small wicks."""
    return mk(i, hi, hi + 0.2, lo - 0.2, lo, step_s)
