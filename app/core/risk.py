"""Risk → Deriv multiplier stake conversion (Option A execution).

Deriv multiplier P/L:  pnl = stake × multiplier × (price_change / entry_price)
(direction-signed; MULTUP profits when price rises, MULTDOWN when it falls).

We choose stake so that the loss when price hits the SL equals
`risk_per_trade` (e.g. 1%) of the account balance, then express SL/TP to the
API as currency amounts via `limit_order` — so the contract closes at exactly
the planned price levels with the planned money at risk.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.entry import RR, TradePlan


@dataclass(frozen=True)
class StakePlan:
    stake: float          # currency units to put on the multiplier contract
    multiplier: int
    risk_amount: float    # currency loss if SL is hit  (= stake·mult·risk_frac)
    sl_amount: float      # limit_order.stop_loss  (positive currency amount)
    tp_amount: float      # limit_order.take_profit (positive currency amount)


def compute_stake(
    plan: TradePlan,
    balance: float,
    risk_per_trade: float,
    multiplier: int,
    min_stake: float = 1.0,
    stake_decimals: int = 2,
) -> StakePlan:
    """Size a multiplier contract so loss-at-SL = risk_per_trade × balance.

    price_move_frac = |entry − sl| / entry
    loss_at_sl      = stake × multiplier × price_move_frac  = risk_amount
    ⇒ stake         = risk_amount / (multiplier × price_move_frac)
    """
    if balance <= 0:
        raise ValueError("balance must be positive")
    if not 0 < risk_per_trade < 1:
        raise ValueError("risk_per_trade must be a fraction in (0, 1)")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")

    risk_amount = balance * risk_per_trade
    move_frac = plan.risk / plan.entry
    if move_frac <= 0:
        raise ValueError("entry and SL must differ")

    stake = round(risk_amount / (multiplier * move_frac), stake_decimals)
    if stake < min_stake:
        # Deriv rejects stakes under the minimum; caller must surface this.
        raise ValueError(
            f"required stake {stake} is below the minimum {min_stake}; "
            "increase multiplier or risk, or skip the trade"
        )

    # Actual currency at risk after stake rounding:
    actual_risk = stake * multiplier * move_frac
    return StakePlan(
        stake=stake,
        multiplier=multiplier,
        risk_amount=round(actual_risk, 2),
        sl_amount=round(actual_risk, 2),
        tp_amount=round(actual_risk * RR, 2),
    )
