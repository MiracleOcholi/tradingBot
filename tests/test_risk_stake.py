"""Risk→stake conversion: loss at SL must equal risk_per_trade × balance."""
import pytest

from app.core.entry import build_plan
from app.core.models import Side
from app.core.risk import compute_stake


def test_loss_at_sl_equals_one_percent():
    plan = build_plan(Side.BUY, entry=6300.0, sl=6237.0)   # 1% price move
    sp = compute_stake(plan, balance=1000.0, risk_per_trade=0.01, multiplier=100)
    # loss at SL = stake × mult × move_frac  ≈ $10 = 1% of $1000
    move_frac = plan.risk / plan.entry
    assert sp.stake * sp.multiplier * move_frac == pytest.approx(10.0, rel=1e-3)
    assert sp.sl_amount == pytest.approx(10.0, rel=1e-3)


def test_tp_amount_is_four_times_sl_amount():
    plan = build_plan(Side.SELL, entry=100000.0, sl=100500.0)
    sp = compute_stake(plan, balance=2500.0, risk_per_trade=0.01, multiplier=200)
    assert sp.tp_amount == pytest.approx(4 * sp.sl_amount, rel=1e-6)


def test_smaller_move_needs_bigger_stake():
    tight = build_plan(Side.BUY, 1000.0, 999.0)    # 0.1% move
    wide = build_plan(Side.BUY, 1000.0, 990.0)     # 1.0% move
    s_tight = compute_stake(tight, 1000.0, 0.01, 100)
    s_wide = compute_stake(wide, 1000.0, 0.01, 100)
    assert s_tight.stake > s_wide.stake


def test_min_stake_violation_raises():
    # Huge move + big multiplier → required stake under the $1 minimum.
    plan = build_plan(Side.BUY, 100.0, 80.0)       # 20% move
    with pytest.raises(ValueError, match="below the minimum"):
        compute_stake(plan, balance=100.0, risk_per_trade=0.01, multiplier=100)


def test_input_validation():
    plan = build_plan(Side.BUY, 100.0, 99.0)
    with pytest.raises(ValueError):
        compute_stake(plan, balance=0, risk_per_trade=0.01, multiplier=100)
    with pytest.raises(ValueError):
        compute_stake(plan, balance=100, risk_per_trade=0, multiplier=100)
    with pytest.raises(ValueError):
        compute_stake(plan, balance=100, risk_per_trade=0.01, multiplier=0)
