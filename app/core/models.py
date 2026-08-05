"""Shared domain models for the MSnR engine (PDF §2–§8)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class Formation(str, Enum):
    """How the SNR formed (PDF §2). Role can later flip via SBR/RBS."""
    TRAD_R = "TRAD_R"  # bullish → bearish
    TRAD_S = "TRAD_S"  # bearish → bullish
    OC_R = "OC_R"      # bearish → bearish ("kissing candles")
    OC_S = "OC_S"      # bullish → bullish


class SetupState(str, Enum):
    """Setup state machine (PDF §8.3)."""
    IDLE = "IDLE"
    DIRECTION_SET = "DIRECTION_SET"
    AWAIT_TAP = "AWAIT_TAP"
    REJECTION_PENDING = "REJECTION_PENDING"
    M15_BREAK_PENDING = "M15_BREAK_PENDING"
    H1_ENGULF_PENDING = "H1_ENGULF_PENDING"
    SETUP_CONFIRMED = "SETUP_CONFIRMED"


@dataclass(frozen=True)
class Candle:
    """A completed OHLC candle. `ts` is the candle's open time (UTC)."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)


@dataclass
class SNRLevel:
    """A single-price-point SNR, marked at the 1st candle's close (PDF §4).

    The marking price NEVER moves; only `role` flips on body-close breaks:
    break_count 0 = original role, 1 = SBR/RBS flip, 2 = Left Shoulder.
    """
    symbol: str
    timeframe: str
    price: float
    formation: Formation
    role: str                 # 'S' | 'R' (current role)
    first_candle_at: datetime
    break_count: int = 0
    fresh: bool = True
    touches: int = 0
    played: bool = False
    active: bool = True
    id: str | None = None
