"""Edit-one-value recompute math (PDF §8.4): 1:4 ratio + stop relationship."""
import pytest

from app.core.entry import RR, TradePlan, build_plan, recompute, validate
from app.core.models import Side


def test_build_buy_plan_tp_at_1_to_4():
    p = build_plan(Side.BUY, entry=100.0, sl=98.0)
    assert p.tp == pytest.approx(108.0)          # risk 2 → reward 8
    assert p.reward == pytest.approx(RR * p.risk)


def test_build_sell_plan_tp_at_1_to_4():
    p = build_plan(Side.SELL, entry=100.0, sl=102.5)
    assert p.tp == pytest.approx(90.0)           # risk 2.5 → reward 10
    assert p.reward == pytest.approx(RR * p.risk)


def test_build_rejects_wrong_side_sl():
    with pytest.raises(ValueError):
        build_plan(Side.BUY, entry=100.0, sl=101.0)   # SL above entry on a BUY
    with pytest.raises(ValueError):
        build_plan(Side.SELL, entry=100.0, sl=99.0)   # SL below entry on a SELL
    with pytest.raises(ValueError):
        build_plan(Side.BUY, entry=100.0, sl=100.0)   # zero risk


def test_edit_entry_keeps_structural_sl_recomputes_tp():
    p = build_plan(Side.BUY, 100.0, 98.0)
    q = recompute(p, "entry", 99.0)
    assert q.sl == 98.0                              # structural stop untouched
    assert q.risk == pytest.approx(1.0)
    assert q.tp == pytest.approx(99.0 + 4.0)         # 1:4 re-held


def test_edit_sl_keeps_entry_recomputes_tp():
    p = build_plan(Side.SELL, 200.0, 204.0)
    q = recompute(p, "sl", 202.0)
    assert q.entry == 200.0
    assert q.tp == pytest.approx(200.0 - 4 * 2.0)


def test_edit_tp_recomputes_sl_holding_1_to_4():
    p = build_plan(Side.BUY, 100.0, 98.0)            # tp 108
    q = recompute(p, "tp", 104.0)                    # reward 4 → risk 1
    assert q.entry == 100.0
    assert q.sl == pytest.approx(99.0)
    assert q.reward == pytest.approx(RR * q.risk)


def test_edit_tp_on_wrong_side_rejected():
    p = build_plan(Side.BUY, 100.0, 98.0)
    with pytest.raises(ValueError):
        recompute(p, "tp", 95.0)                     # TP below entry on a BUY


def test_edit_entry_crossing_sl_rejected():
    p = build_plan(Side.BUY, 100.0, 98.0)
    with pytest.raises(ValueError):
        recompute(p, "entry", 97.0)                  # entry below its stop


def test_unknown_field_rejected():
    p = build_plan(Side.BUY, 100.0, 98.0)
    with pytest.raises(ValueError):
        recompute(p, "stake", 50.0)


def test_validate_accepts_correct_sell():
    validate(TradePlan(Side.SELL, entry=100.0, sl=101.0, tp=96.0))
