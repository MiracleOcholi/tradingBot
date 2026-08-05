"""Option A execution — bot-emulated pending orders → Deriv multipliers.

On Telegram Accept: arm a virtual pending order (virtual_orders row), watch
the Deriv tick stream, and the instant a tick touches the entry level, buy a
market multiplier (MULTUP for BUY / MULTDOWN for SELL) with limit_order
stop_loss / take_profit converted from price levels to currency amounts via
app.core.risk.compute_stake — preserving loss-at-SL = risk_per_trade of
balance and the fixed 1:4 RR.

Guards enforced at FIRE time, before ANY buy leaves the bot:
- config.kill_switch is ON (master enable),
- open trades < config.max_open_trades,
- realised loss today < config.daily_loss_cap × balance,
- the order's account_mode matches config.account_mode,
- an account token for that mode exists in the encrypted secrets store.
A guard failure CANCELS the order (the touch moment has passed) and says why
on Telegram.

Lifecycle housekeeping:
- entry not touched within ARM_TTL_H hours → EXPIRED;
- a tick crossing the TP level before entry is touched → EXPIRED (the move
  played out without us);
- armed orders are reloaded from Supabase on boot and tick subscriptions
  re-established (stateless across sleeps/redeploys).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.core.entry import TradePlan
from app.core.models import Side
from app.core.risk import compute_stake
from app.services import crypto
from app.services.supabase import SupabaseClient, get_db
from app.services.telegram import get_telegram

log = logging.getLogger("maverick.execution")

ARM_TTL_H = 24
MAX_STAKE_FRACTION = 0.2   # prefer the smallest multiplier keeping stake ≤ 20% of balance


class ExecutionService:
    def __init__(self, db: SupabaseClient | None = None) -> None:
        self.db = db or get_db()
        self.market = None                     # wired by the watcher
        self.armed: dict[str, dict] = {}       # vo_id -> virtual_orders row
        self.contract_to_trade: dict[int, str] = {}
        self._mult_cache: dict[str, list[int]] = {}
        self._firing: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def deriv(self):
        return self.market.deriv if self.market else None

    # ---------------------------------------------------------------- boot
    async def load(self) -> None:
        rows = await self.db.select("virtual_orders", "status=eq.ARMED")
        self.armed = {r["id"]: r for r in rows}
        open_trades = await self.db.select("trades", "status=eq.OPEN")
        for t in open_trades:
            if t.get("contract_id"):
                self.contract_to_trade[int(t["contract_id"])] = t["id"]
        if self.deriv:
            for symbol in {r["symbol"] for r in self.armed.values()}:
                await self.deriv.subscribe_ticks(symbol)
            for cid in self.contract_to_trade:
                await self.deriv.watch_contract(cid)
        log.info("execution loaded: %d armed, %d open contracts",
                 len(self.armed), len(self.contract_to_trade))

    # ---------------------------------------------------------------- arming
    async def arm(self, signal: dict) -> dict:
        vo = (await self.db.insert("virtual_orders", {
            "signal_id": signal["id"],
            "symbol": signal["symbol"],
            "side": signal["side"],
            "account_mode": signal["account_mode"],
            "entry_price": signal["entry"],
            "sl_price": signal["sl"],
            "tp_price": signal["tp"],
        }))[0]
        self.armed[vo["id"]] = vo
        if self.deriv:
            await self.deriv.subscribe_ticks(signal["symbol"])
        log.info("armed virtual order %s %s @ %s", vo["symbol"], vo["side"], vo["entry_price"])
        return vo

    async def cancel(self, vo_id: str, status: str, reason: str) -> None:
        vo = self.armed.pop(vo_id, None)
        await self.db.update(
            "virtual_orders", f"id=eq.{vo_id}", {"status": status, "reason": reason}
        )
        if vo:
            await self.db.update(
                "signals", f"id=eq.{vo['signal_id']}",
                {"status": "EXPIRED", "updated_at": "now()"},
            )
            await get_telegram().send_text(
                f"⚪️ Pending order cancelled — {vo['symbol']} {vo['side']}: {reason}"
            )
            await self._maybe_unsubscribe(vo["symbol"])

    async def _maybe_unsubscribe(self, symbol: str) -> None:
        if self.deriv and not any(v["symbol"] == symbol for v in self.armed.values()):
            await self.deriv.unsubscribe_ticks(symbol)

    # ---------------------------------------------------------------- ticks
    async def on_tick(self, symbol: str, quote: float, epoch: int) -> None:
        now = datetime.now(UTC)
        for vo_id, vo in list(self.armed.items()):
            if vo["symbol"] != symbol or vo_id in self._firing:
                continue
            side = Side(vo["side"])
            entry, tp = float(vo["entry_price"]), float(vo["tp_price"])

            armed_at = datetime.fromisoformat(vo["armed_at"].replace("Z", "+00:00"))
            if now - armed_at > timedelta(hours=ARM_TTL_H):
                await self.cancel(vo_id, "EXPIRED", f"entry untouched for {ARM_TTL_H}h")
                continue
            # Missed move: for a BUY (entry below market) the trade idea is
            # exhausted if price runs UP through TP without touching entry;
            # SELL is the mirror.
            tp_crossed = quote >= tp if side is Side.BUY else quote <= tp
            if tp_crossed:
                await self.cancel(vo_id, "EXPIRED", "TP level traded before entry")
                continue

            touched = quote <= entry if side is Side.BUY else quote >= entry
            if touched:
                self._firing.add(vo_id)
                try:
                    await self._fire(vo, quote)
                finally:
                    self._firing.discard(vo_id)

    # ---------------------------------------------------------------- firing
    async def _guards(self, vo: dict, cfg: dict) -> str | None:
        if not cfg.get("kill_switch"):
            return "kill switch is OFF"
        if vo["account_mode"] != cfg.get("account_mode"):
            return f"order is {vo['account_mode']} but account is {cfg.get('account_mode')}"
        open_trades = await self.db.select(
            "trades", f"status=eq.OPEN&account_mode=eq.{cfg['account_mode']}"
        )
        if len(open_trades) >= int(cfg.get("max_open_trades", 1)):
            return f"max open trades ({cfg.get('max_open_trades')}) reached"
        return None

    async def _daily_loss_exceeded(self, cfg: dict, balance: float) -> bool:
        today = datetime.now(UTC).date().isoformat()
        closed = await self.db.select(
            "trades",
            f"account_mode=eq.{cfg['account_mode']}&closed_at=gte.{today}T00:00:00Z",
        )
        realised = sum(float(t["pnl"] or 0) for t in closed)
        return realised <= -float(cfg.get("daily_loss_cap", 0.05)) * balance

    async def _pick_multiplier(self, symbol: str, plan: TradePlan, balance: float,
                               risk_per_trade: float) -> int | None:
        """Smallest multiplier keeping stake ≤ MAX_STAKE_FRACTION × balance;
        else the largest one whose stake still clears the $1 minimum."""
        mults = await self._multipliers_for(symbol)
        if not mults:
            return None
        risk_amount = balance * risk_per_trade
        move_frac = plan.risk / plan.entry
        for m in sorted(mults):
            stake = risk_amount / (m * move_frac)
            if stake <= balance * MAX_STAKE_FRACTION:
                return m if stake >= 1.0 else None
        return None

    async def _multipliers_for(self, symbol: str) -> list[int]:
        if symbol in self._mult_cache:
            return self._mult_cache[symbol]
        try:
            resp = await self.deriv.send({"contracts_for": symbol, "product_type": "basic"})
            mults: set[int] = set()
            for c in resp.get("contracts_for", {}).get("available", []):
                if c.get("contract_type") in ("MULTUP", "MULTDOWN"):
                    for m in c.get("multiplier_range", []):
                        mults.add(int(m))
            self._mult_cache[symbol] = sorted(mults)
        except Exception:
            log.exception("contracts_for failed for %s", symbol)
            return []
        return self._mult_cache[symbol]

    async def _fire(self, vo: dict, quote: float) -> None:
        async with self._lock:
            if vo["id"] not in self.armed:
                return
            cfg = await self.db.get_config()
            reason = await self._guards(vo, cfg)
            if reason:
                await self.cancel(vo["id"], "CANCELLED", reason)
                return

            token_row = await self.db.select(
                "secrets", f"name=eq.deriv_token_{cfg['account_mode'].lower()}"
            )
            if not token_row:
                await self.cancel(vo["id"], "CANCELLED",
                                  f"no {cfg['account_mode']} token stored")
                return

            side = Side(vo["side"])
            plan = TradePlan(side=side, entry=float(vo["entry_price"]),
                             sl=float(vo["sl_price"]), tp=float(vo["tp_price"]))
            try:
                token = crypto.decrypt(token_row[0]["value_encrypted"])
                auth = await self.deriv.authorize(token)
                # The account the broker actually authorized must match the
                # mode we believe we are in. Tokens can map to several
                # accounts, and the data socket may have authorized with a
                # different one — never place an order on a real account
                # while the dashboard says DEMO.
                is_virtual = bool(auth.get("is_virtual"))
                wants_virtual = cfg["account_mode"] == "DEMO"
                if is_virtual != wants_virtual:
                    await self.cancel(
                        vo["id"], "CANCELLED",
                        f"refusing to trade: mode is {cfg['account_mode']} but the token "
                        f"authorized {auth.get('loginid')} "
                        f"({'virtual' if is_virtual else 'REAL'})",
                    )
                    return
                currency = auth.get("currency", "USD")
                bal_resp = await self.deriv.send({"balance": 1})
                balance = float(bal_resp.get("balance", {}).get("balance") or
                                auth.get("balance") or 0)
                if balance <= 0:
                    raise RuntimeError("zero balance")
                if await self._daily_loss_exceeded(cfg, balance):
                    await self.cancel(vo["id"], "CANCELLED", "daily loss cap reached")
                    return

                risk = float(cfg.get("risk_per_trade", 0.01))
                mult = await self._pick_multiplier(vo["symbol"], plan, balance, risk)
                if mult is None:
                    await self.cancel(vo["id"], "CANCELLED",
                                      "no viable multiplier/stake for this risk")
                    return
                stake_plan = compute_stake(plan, balance, risk, mult)

                contract_type = "MULTUP" if side is Side.BUY else "MULTDOWN"
                prop = await self.deriv.send({
                    "proposal": 1,
                    "amount": stake_plan.stake,
                    "basis": "stake",
                    "contract_type": contract_type,
                    "currency": currency,
                    "multiplier": stake_plan.multiplier,
                    "symbol": vo["symbol"],
                    "limit_order": {
                        "stop_loss": stake_plan.sl_amount,
                        "take_profit": stake_plan.tp_amount,
                    },
                })
                proposal = prop["proposal"]
                buy = await self.deriv.send(
                    {"buy": proposal["id"], "price": stake_plan.stake}
                )
                contract_id = int(buy["buy"]["contract_id"])
            except Exception as e:  # incl. DerivAPIError / ConnectionError
                log.exception("fire failed for %s", vo["id"])
                await self.cancel(vo["id"], "CANCELLED", f"execution error: {e}")
                return

            self.armed.pop(vo["id"], None)
            await self.db.update("virtual_orders", f"id=eq.{vo['id']}", {
                "status": "TRIGGERED",
                "triggered_at": datetime.now(UTC).isoformat(),
            })
            trade = (await self.db.insert("trades", {
                "signal_id": vo["signal_id"],
                "account_mode": vo["account_mode"],
                "symbol": vo["symbol"],
                "side": vo["side"],
                "contract_id": str(contract_id),
                "stake": stake_plan.stake,
                "multiplier": stake_plan.multiplier,
                "entry_price": quote,
                "sl_price": vo["sl_price"],
                "tp_price": vo["tp_price"],
                "raw": {"buy": buy.get("buy", {}), "risk_amount": stake_plan.risk_amount},
            }))[0]
            await self.db.update("signals", f"id=eq.{vo['signal_id']}",
                                 {"status": "FILLED", "updated_at": "now()"})
            self.contract_to_trade[contract_id] = trade["id"]
            await self.deriv.watch_contract(contract_id)
            await self._maybe_unsubscribe(vo["symbol"])
            await get_telegram().send_text(
                f"🎯 <b>FILLED</b> {vo['symbol']} {vo['side']} ({vo['account_mode']})\n"
                f"stake <code>{stake_plan.stake}</code> × x{stake_plan.multiplier} @ <code>{quote}</code>\n"
                f"risk <code>{stake_plan.sl_amount}</code> → target <code>{stake_plan.tp_amount}</code> (1:4)"
            )

    # ---------------------------------------------------------------- outcomes
    async def on_contract(self, poc: dict) -> None:
        contract_id = int(poc.get("contract_id", 0))
        trade_id = self.contract_to_trade.get(contract_id)
        if trade_id is None or not poc.get("is_sold"):
            return
        pnl = float(poc.get("profit") or 0)
        status = "WON" if pnl >= 0 else "LOST"
        await self.db.update("trades", f"id=eq.{trade_id}", {
            "status": status,
            "pnl": pnl,
            "closed_at": datetime.now(UTC).isoformat(),
            "raw": {"final": {k: poc.get(k) for k in
                              ("sell_price", "buy_price", "profit", "status")}},
        })
        rows = await self.db.select("trades", f"id=eq.{trade_id}")
        if rows and rows[0].get("signal_id"):
            await self.db.update("signals", f"id=eq.{rows[0]['signal_id']}",
                                 {"status": "CLOSED", "updated_at": "now()"})
        self.contract_to_trade.pop(contract_id, None)
        if self.deriv:
            self.deriv.unwatch_contract(contract_id)
        emoji = "🟢" if pnl >= 0 else "🔴"
        await get_telegram().send_text(
            f"{emoji} <b>CLOSED {status}</b> — contract {contract_id}: "
            f"P/L <code>{pnl:+.2f}</code>"
        )

    def status(self) -> dict:
        return {"armed": len(self.armed), "open_contracts": len(self.contract_to_trade)}


_execution: ExecutionService | None = None


def get_execution() -> ExecutionService:
    global _execution
    if _execution is None:
        _execution = ExecutionService()
    return _execution
