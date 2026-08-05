"""Phase F: deterministic stats, excursion metrics, suggestion thresholds."""
from datetime import datetime, timezone

import pytest

from app.services.analytics import (
    ENTRY_HINT_SAFETY,
    ExcursionTracker,
    build_suggestions,
    summarize,
)
from tests.helpers import mk


def trade(pnl, symbol="R_10", side="BUY", signal_id=None, risk=10.0, entry_price=None):
    return {
        "status": "WON" if pnl > 0 else "LOST",
        "pnl": pnl, "symbol": symbol, "side": side,
        "signal_id": signal_id, "raw": {"risk_amount": risk},
        "entry_price": entry_price,
    }


def signal(sid, status="CLOSED", engulf=1, entry=100.0, excursion=None, mock=False):
    ctx = {"engulf_type": engulf}
    if excursion:
        ctx["excursion"] = excursion
    return {
        "id": sid, "status": status, "is_mock": mock, "entry": entry,
        "context": ctx, "symbol": "R_10", "side": "BUY",
    }


# ---------------------------------------------------------------- summarize
def test_summarize_overall_and_buckets():
    sigs = [signal("s1"), signal("s2", engulf=2)]
    trades = [
        trade(40.0, signal_id="s1", entry_price=100.5),
        trade(-10.0, signal_id="s2", side="SELL", entry_price=99.5),
    ]
    vos = [
        {"status": "EXPIRED", "reason": "TP level traded before entry"},
        {"status": "CANCELLED", "reason": "kill switch is OFF"},
        {"status": "TRIGGERED", "reason": None},
    ]
    stats = summarize(sigs, trades, vos)
    assert stats["overall"]["trades"] == 2
    assert stats["overall"]["win_rate"] == 0.5
    assert stats["overall"]["pnl"] == 30.0
    # R multiples: +40/10 = +4R, -10/10 = -1R → avg +1.5R
    assert stats["overall"]["avg_r"] == pytest.approx(1.5)
    assert stats["by_engulf_type"]["type_1"]["trades"] == 1
    assert stats["by_engulf_type"]["type_2"]["trades"] == 1
    assert stats["cancel_reasons"]["kill switch is OFF"] == 1
    # slippage: |100.5-100| and |99.5-100| → 0.5 avg
    assert stats["avg_fill_slippage"] == pytest.approx(0.5)


def test_summarize_empty_is_safe():
    stats = summarize([], [], [])
    assert stats["overall"]["trades"] == 0
    assert stats["overall"]["win_rate"] is None
    assert stats["avg_fill_slippage"] is None


# ---------------------------------------------------------------- excursion
class _NullDB:
    async def update(self, *a, **k):
        return []

    async def select(self, *a, **k):
        return []


def make_rec(side="BUY"):
    t = ExcursionTracker(db=_NullDB())
    sig = {
        "id": "s1", "symbol": "R_10", "side": side,
        "entry": 105.0 if side == "BUY" else 115.0,
        "tp": 125.0 if side == "BUY" else 95.0,
        "order_block": {"high": 105.0, "low": 103.0} if side == "BUY"
                       else {"high": 117.0, "low": 115.0},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "context": {},
    }
    t.register(sig)
    return t


async def test_buy_penetration_and_flags():
    t = make_rec("BUY")
    # dips to 104 = 50% of the 105-103 zone; never reaches entry... wait,
    # entry IS the proximal edge 105 → low 104 touches it. TP not seen.
    await t.on_m15_close("R_10", mk(0, 106, 107, 104.0, 106.5))
    m = t.active["s1"]["context"]["excursion"]
    assert m["ob_penetration"] == pytest.approx(0.5)
    assert m["entry_touched"] is True
    assert m["tp_seen"] is False
    # later candle runs to TP
    await t.on_m15_close("R_10", mk(1, 106.5, 125.5, 106, 125.0))
    m = t.active["s1"]["context"]["excursion"]
    assert m["tp_seen"] is True


async def test_sell_penetration_mirror():
    t = make_rec("SELL")                       # zone 115-117, entry 115 (low edge)
    await t.on_m15_close("R_10", mk(0, 114, 116.0, 113, 114.5))  # up to 116 = 50%
    m = t.active["s1"]["context"]["excursion"]
    assert m["ob_penetration"] == pytest.approx(0.5)
    assert m["entry_touched"] is True
    await t.on_m15_close("R_10", mk(1, 114.5, 115, 94.5, 95.0))
    assert t.active["s1"]["context"]["excursion"]["tp_seen"] is True


async def test_unrelated_symbol_ignored():
    t = make_rec("BUY")
    await t.on_m15_close("JD10", mk(0, 1, 2, 0.5, 1.5))
    assert "excursion" not in t.active["s1"]["context"]


# ---------------------------------------------------------------- suggestions
def missed_signal(sid, depth):
    return signal(
        sid, status="EXPIRED",
        excursion={"ob_penetration": depth, "tp_seen": True, "entry_touched": False},
    )


def test_entry_depth_suggestion_fires_at_threshold():
    sigs = [missed_signal(f"s{i}", d) for i, d in enumerate([0.3, 0.4, 0.5, 0.6, 0.7])]
    out = build_suggestions(sigs, {"by_engulf_type": {}, "by_symbol": {}})
    assert len(out) == 1 and out[0]["kind"] == "entry_depth"
    assert out[0]["suggested_depth"] == pytest.approx(0.5 * ENTRY_HINT_SAFETY)
    assert out[0]["suggested_depth"] <= 1.0        # never outside the zone


def test_entry_depth_needs_min_sample():
    sigs = [missed_signal(f"s{i}", 0.5) for i in range(4)]   # below threshold of 5
    assert build_suggestions(sigs, {"by_engulf_type": {}, "by_symbol": {}}) == []


def test_engulf_type_comparison_needs_sample_and_gap():
    small = {"by_engulf_type": {
        "type_1": {"trades": 5, "win_rate": 0.8, "pnl": 1, "avg_r": 1},
        "type_2": {"trades": 30, "win_rate": 0.3, "pnl": 1, "avg_r": 1},
    }, "by_symbol": {}}
    assert build_suggestions([], small) == []                # type_1 sample too small

    close_rates = {"by_engulf_type": {
        "type_1": {"trades": 20, "win_rate": 0.50, "pnl": 1, "avg_r": 1},
        "type_2": {"trades": 20, "win_rate": 0.55, "pnl": 1, "avg_r": 1},
    }, "by_symbol": {}}
    assert build_suggestions([], close_rates) == []          # gap under 15pts

    clear = {"by_engulf_type": {
        "type_1": {"trades": 20, "win_rate": 0.30, "pnl": 1, "avg_r": 1},
        "type_2": {"trades": 20, "win_rate": 0.60, "pnl": 1, "avg_r": 1},
    }, "by_symbol": {}}
    out = build_suggestions([], clear)
    assert len(out) == 1 and "Type 2" in out[0]["message"]


def test_symbol_drag_rule():
    stats = {"by_engulf_type": {}, "by_symbol": {
        "JD75": {"trades": 12, "win_rate": 0.2, "pnl": -80.0, "avg_r": -0.8},
        "R_10": {"trades": 12, "win_rate": 0.5, "pnl": 40.0, "avg_r": 0.5},
    }}
    out = build_suggestions([], stats)
    assert len(out) == 1 and "JD75" in out[0]["message"]
