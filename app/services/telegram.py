"""Telegram bot API: signal cards (Accept/Reject/Edit), reminder cards.

Callback data grammar (≤64 bytes):
  sig:<uuid>:accept | sig:<uuid>:reject | sig:<uuid>:edit
  sig:<uuid>:edit:entry|sl|tp          (choose which field to edit)
  sig:<uuid>:edit:cancel
  rem:<uuid>:done                      (key-rotation "✅ Done — rotated")
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import SYMBOL_LABELS, get_settings

log = logging.getLogger("maverick.telegram")


class Telegram:
    def __init__(self) -> None:
        s = get_settings()
        self._token = s.telegram_bot_token
        self._chat_id = s.telegram_chat_id
        self._client = httpx.AsyncClient(timeout=15.0)

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict) -> dict | None:
        if not self.enabled:
            log.warning("telegram not configured; dropping %s", method)
            return None
        r = await self._client.post(
            f"https://api.telegram.org/bot{self._token}/{method}", json=payload
        )
        data = r.json()
        if not data.get("ok"):
            log.error("telegram %s failed: %s", method, data)
            return None
        return data["result"]

    # ---- signal cards ------------------------------------------------------
    @staticmethod
    def signal_text(sig: dict) -> str:
        label = SYMBOL_LABELS.get(sig["symbol"], sig["symbol"])
        mock = " · MOCK" if sig.get("is_mock") else ""
        risk = abs(float(sig["entry"]) - float(sig["sl"]))
        return (
            f"🛩 <b>MAVERICK SIGNAL</b>{mock}\n"
            f"<b>{label}</b> ({sig['symbol']}) — <b>{sig['side']}</b>\n"
            f"Account: <b>{sig['account_mode']}</b>\n"
            f"\n"
            f"Entry: <code>{sig['entry']}</code>\n"
            f"SL:    <code>{sig['sl']}</code>  (risk {risk:.4f})\n"
            f"TP:    <code>{sig['tp']}</code>  (1:4)\n"
        )

    @staticmethod
    def signal_keyboard(signal_id: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Accept", "callback_data": f"sig:{signal_id}:accept"},
                {"text": "❌ Reject", "callback_data": f"sig:{signal_id}:reject"},
                {"text": "✏️ Edit", "callback_data": f"sig:{signal_id}:edit"},
            ]]
        }

    @staticmethod
    def edit_keyboard(signal_id: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "Entry", "callback_data": f"sig:{signal_id}:edit:entry"},
                {"text": "SL", "callback_data": f"sig:{signal_id}:edit:sl"},
                {"text": "TP", "callback_data": f"sig:{signal_id}:edit:tp"},
                {"text": "↩︎", "callback_data": f"sig:{signal_id}:edit:cancel"},
            ]]
        }

    async def send_signal_card(self, sig: dict) -> int | None:
        res = await self._call("sendMessage", {
            "chat_id": self._chat_id,
            "text": self.signal_text(sig),
            "parse_mode": "HTML",
            "reply_markup": self.signal_keyboard(sig["id"]),
        })
        return res["message_id"] if res else None

    async def update_signal_card(self, sig: dict, note: str = "", keyboard: dict | None = None) -> None:
        if not sig.get("telegram_message_id"):
            return
        text = self.signal_text(sig) + (f"\n{note}" if note else "")
        await self._call("editMessageText", {
            "chat_id": self._chat_id,
            "message_id": sig["telegram_message_id"],
            "text": text,
            "parse_mode": "HTML",
            **({"reply_markup": keyboard} if keyboard is not None else {}),
        })

    async def send_text(self, text: str) -> int | None:
        res = await self._call("sendMessage", {
            "chat_id": self._chat_id, "text": text, "parse_mode": "HTML",
        })
        return res["message_id"] if res else None

    # ---- reminders -----------------------------------------------------------
    async def send_reminder_card(self, rem: dict) -> int | None:
        nth = rem.get("resend_count", 0)
        nag = "" if nth == 0 else f"  (reminder {nth + 1})"
        res = await self._call("sendMessage", {
            "chat_id": self._chat_id,
            "text": (
                f"🔑 <b>KEY ROTATION DUE</b>{nag}\n"
                f"Rotate the Deriv API token(s). Due: <b>{rem['next_due']}</b>\n"
                f"Tap when finished:"
            ),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Done — rotated", "callback_data": f"rem:{rem['id']}:done"}
            ]]},
        })
        return res["message_id"] if res else None

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {
            "callback_query_id": callback_id, **({"text": text} if text else {}),
        })


_tg: Telegram | None = None


def get_telegram() -> Telegram:
    global _tg
    if _tg is None:
        _tg = Telegram()
    return _tg
