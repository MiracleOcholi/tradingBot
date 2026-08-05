"""POST /telegram — Telegram webhook (callback buttons + edit-value replies).

Every request must carry X-Telegram-Bot-Api-Secret-Token matching
TELEGRAM_WEBHOOK_SECRET (set via setWebhook secret_token).
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import get_settings
from app.services import reminders, signals
from app.services.telegram import get_telegram

log = logging.getLogger("maverick.webhook")
router = APIRouter()


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    secret = get_settings().telegram_webhook_secret
    if not secret or not hmac.compare_digest(x_telegram_bot_api_secret_token, secret):
        raise HTTPException(status_code=403, detail="bad secret token")

    update = await request.json()

    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
    elif "message" in update:
        await _handle_message(update["message"])
    return {"ok": True}


async def _handle_callback(cb: dict) -> None:
    tg = get_telegram()
    data = cb.get("data", "")
    toast = ""
    try:
        parts = data.split(":")
        if parts[0] == "sig" and len(parts) == 3:
            toast = await signals.handle_action(parts[1], parts[2])
        elif parts[0] == "sig" and len(parts) == 4 and parts[2] == "edit":
            toast = await signals.start_edit(parts[1], parts[3])
        elif parts[0] == "rem" and len(parts) == 3 and parts[2] == "done":
            next_due = await reminders.mark_done(parts[1])
            toast = f"Rotation logged. Next due {next_due.isoformat()}"
            await tg.send_text(f"🔑 Rotation recorded — next due <b>{next_due.isoformat()}</b>.")
        else:
            toast = "Unknown action"
    except Exception:
        log.exception("callback handling failed: %s", data)
        toast = "Error — check logs"
    await tg.answer_callback(cb["id"], toast)


async def _handle_message(msg: dict) -> None:
    """Plain messages are only meaningful as edit-value replies."""
    text = (msg.get("text") or "").strip()
    if not text or text.startswith("/"):
        return
    reply = await signals.apply_edit_value(text)
    if reply:
        await get_telegram().send_text(reply)
