"""Option A execution — Phase D implementation target.

On Telegram Accept: arm a virtual pending order (virtual_orders row), watch
the Deriv tick stream, and the instant a tick touches the entry level, buy a
market multiplier (MULTUP for BUY / MULTDOWN for SELL) with limit_order
stop_loss / take_profit converted from price levels to currency amounts via
app.core.risk.compute_stake — preserving loss-at-SL = risk_per_trade of
balance and the fixed 1:4 RR.

Guards enforced here before ANY buy leaves the bot:
- config.kill_switch is ON,
- open trades < config.max_open_trades,
- daily realised loss < config.daily_loss_cap,
- the account_mode on the order matches the authorized Deriv account.

Virtual orders persist in Supabase and are re-armed on boot.
"""
