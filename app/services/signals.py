"""Signal lifecycle: creation (mock for Phase A), Accept/Reject/Edit handling."""
from __future__ import annotations

import logging
import random

from app.config import WATCHLIST
from app.core.entry import TradePlan, build_plan, recompute
from app.core.models import Side
from app.services.supabase import get_db
from app.services.telegram import get_telegram

log = logging.getLogger("maverick.signals")

# Rough price scales per symbol so mock signals look plausible on the card.
MOCK_PRICE = {
    "R_10": 6300.0, "R_50": 220.0, "R_75": 100000.0, "1HZ150V": 1300.0,
    "JD10": 9200.0, "JD75": 45000.0, "JD100": 62000.0,
}


async def create_mock_signal(symbol: str | None = None) -> dict:
    """Phase A: emit a fake-but-well-formed signal card to exercise the loop."""
    db, tg = get_db(), get_telegram()
    cfg = await db.get_config()

    symbol = symbol or random.choice(WATCHLIST)
    side = random.choice([Side.BUY, Side.SELL])
    base = MOCK_PRICE.get(symbol, 1000.0)
    entry = round(base * random.uniform(0.995, 1.005), 4)
    risk = round(base * random.uniform(0.001, 0.004), 4)
    sl = round(entry - risk if side is Side.BUY else entry + risk, 4)
    plan = build_plan(side, entry, sl)

    sig = await db.create_signal({
        "symbol": symbol,
        "side": side.value,
        "account_mode": cfg["account_mode"],
        "entry": plan.entry,
        "sl": plan.sl,
        "tp": round(plan.tp, 4),
        "is_mock": True,
        "context": {"note": "phase-a mock loop"},
    })
    msg_id = await tg.send_signal_card(sig)
    if msg_id:
        sig = await db.update_signal(sig["id"], {"telegram_message_id": msg_id})
    return sig


async def create_setup_signal(
    symbol: str,
    side: Side,
    entry: float,
    sl: float,
    tp: float,
    order_block: dict | None,
    context: dict,
    expired: bool = False,
) -> dict:
    """A REAL confirmed setup (SETUP_CONFIRMED → Telegram, PDF §8.4).

    `expired=True` marks a setup recovered from stale history replay: it is
    logged for the analytics loop but never alerted or armed.
    """
    db, tg = get_db(), get_telegram()
    cfg = await db.get_config()
    sig = await db.create_signal({
        "symbol": symbol,
        "side": side.value,
        "account_mode": cfg["account_mode"],
        "entry": round(float(entry), 6),
        "sl": round(float(sl), 6),
        "tp": round(float(tp), 6),
        "status": "EXPIRED" if expired else "PENDING",
        "is_mock": False,
        "order_block": order_block,
        "context": context,
    })
    if not expired:
        msg_id = await tg.send_signal_card(sig)
        if msg_id:
            sig = await db.update_signal(sig["id"], {"telegram_message_id": msg_id})
    return sig


def _plan_of(sig: dict) -> TradePlan:
    return TradePlan(
        side=Side(sig["side"]),
        entry=float(sig["entry"]),
        sl=float(sig["sl"]),
        tp=float(sig["tp"]),
    )


async def handle_action(signal_id: str, action: str) -> str:
    """Accept / Reject / start-Edit on a pending signal. Returns toast text."""
    db, tg = get_db(), get_telegram()
    sig = await db.get_signal(signal_id)
    if not sig:
        return "Signal not found"
    if sig["status"] != "PENDING":
        return f"Already {sig['status'].lower()}"

    if action == "accept":
        sig = await db.update_signal(signal_id, {"status": "ACCEPTED", "awaiting_edit_field": None})
        await db.record_decision(signal_id, "ACCEPT")
        if sig["is_mock"]:
            note = "🟢 <b>Accepted.</b> (mock signal — nothing armed)"
        else:
            from app.execution.emulated_pending import get_execution
            cfg = await db.get_config()
            await get_execution().arm(sig)
            note = "🟢 <b>Accepted — virtual pending order ARMED at entry.</b>"
            if not cfg.get("kill_switch"):
                note += "\n⚠️ Kill switch is OFF — the order will be cancelled at touch unless you arm it."
        await tg.update_signal_card(sig, note, keyboard={"inline_keyboard": []})
        return "Accepted"

    if action == "reject":
        sig = await db.update_signal(signal_id, {"status": "REJECTED", "awaiting_edit_field": None})
        await db.record_decision(signal_id, "REJECT")
        await tg.update_signal_card(sig, "🔴 <b>Rejected.</b>", keyboard={"inline_keyboard": []})
        return "Rejected"

    if action == "edit":
        await tg.update_signal_card(sig, "✏️ <b>Edit which value?</b>", keyboard=tg.edit_keyboard(signal_id))
        return "Pick a field"

    return "Unknown action"


async def start_edit(signal_id: str, field: str) -> str:
    db, tg = get_db(), get_telegram()
    sig = await db.get_signal(signal_id)
    if not sig or sig["status"] != "PENDING":
        return "Not editable"
    if field == "cancel":
        sig = await db.update_signal(signal_id, {"awaiting_edit_field": None})
        await tg.update_signal_card(sig, "", keyboard=tg.signal_keyboard(signal_id))
        return "Edit cancelled"
    sig = await db.update_signal(signal_id, {"awaiting_edit_field": field})
    await tg.update_signal_card(
        sig,
        f"✏️ Send the new <b>{field.upper()}</b> as a plain number — "
        f"the other values recompute from the 1:4 ratio.",
        keyboard={"inline_keyboard": [[
            {"text": "↩︎ Cancel edit", "callback_data": f"sig:{signal_id}:edit:cancel"}
        ]]},
    )
    return f"Send new {field}"


async def apply_edit_value(text: str) -> str | None:
    """Handle a plain-number reply when a signal is awaiting an edit value.

    Returns a user-facing error/confirmation string, or None if no signal was
    awaiting input (message ignored).
    """
    db, tg = get_db(), get_telegram()
    sig = await db.latest_awaiting_edit()
    if not sig:
        return None
    try:
        value = float(text.strip().replace(",", ""))
    except ValueError:
        return "That's not a number — send just the new value, e.g. 6301.25"

    field = sig["awaiting_edit_field"]
    old = float(sig[field])
    try:
        new_plan = recompute(_plan_of(sig), field, value)
    except ValueError as e:
        return f"Invalid {field}: {e}"

    sig = await db.update_signal(sig["id"], {
        "entry": new_plan.entry,
        "sl": round(new_plan.sl, 6),
        "tp": round(new_plan.tp, 6),
        "awaiting_edit_field": None,
    })
    await db.record_decision(sig["id"], "EDIT", {
        "field": field, "old": old, "new": value,
        "recomputed": {"entry": new_plan.entry, "sl": new_plan.sl, "tp": new_plan.tp},
    })
    await tg.update_signal_card(
        sig, f"✏️ <b>{field.upper()} updated</b> — others recomputed (1:4 held).",
        keyboard=tg.signal_keyboard(sig["id"]),
    )
    return None  # card already updated; no extra message needed
