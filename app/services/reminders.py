"""Key-rotation reminder engine (HANDOFF-V2 §5).

Cadence: every 3.5 months from 2026-08-05 → 2026-11-20 → 2027-03-05 →
2027-06-20 → 2027-10-05 → 2028-01-20 …  ("+3 months, +15 days" with 30-day
rollover — matches the seeded chain exactly).

Behaviour: when next_due arrives, send a Telegram card with a
"✅ Done — rotated" button. If ignored, resend at +1d, +2d, +4d after the
previous send (3 resends max), then flag `overdue` (amber chip on the
dashboard). On Done: next_due = done_date + 3.5 months, counters reset.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from app.services.supabase import get_db
from app.services.telegram import get_telegram

log = logging.getLogger("maverick.reminders")

RESEND_GAPS = [timedelta(days=1), timedelta(days=2), timedelta(days=4)]


def add_three_and_half_months(d: date) -> date:
    """+3 calendar months, +15 days with 30-day-month rollover.

    Reproduces the agreed chain: Aug 5 → Nov 20 → Mar 5 → Jun 20 → Oct 5 → Jan 20.
    """
    month = d.month + 3
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = d.day + 15
    if day > 30:
        day -= 30
        month += 1
        if month > 12:
            month -= 12
            year += 1
    return date(year, month, day)


async def tick(now: datetime | None = None) -> None:
    """One reminder-engine pass; called periodically by the watcher."""
    now = now or datetime.now(timezone.utc)
    db, tg = get_db(), get_telegram()
    reminders = await db.select("reminders", "")
    for rem in reminders:
        due = date.fromisoformat(rem["next_due"])
        if now.date() < due or rem.get("overdue"):
            continue

        sent_count = rem.get("resend_count", 0)
        last_sent = rem.get("last_sent_at")
        if last_sent is None:
            await _send(db, tg, rem, sent_count=0)
            continue

        last_dt = datetime.fromisoformat(last_sent.replace("Z", "+00:00"))
        if sent_count >= len(RESEND_GAPS):
            # 1 original + 3 resends exhausted → overdue chip
            await db.update("reminders", f"id=eq.{rem['id']}", {"overdue": True})
            log.warning("reminder %s overdue", rem["name"])
        elif now - last_dt >= RESEND_GAPS[sent_count]:
            await _send(db, tg, rem, sent_count=sent_count + 1)


async def _send(db, tg, rem: dict, sent_count: int) -> None:
    rem_view = {**rem, "resend_count": sent_count}
    await tg.send_reminder_card(rem_view)
    await db.update("reminders", f"id=eq.{rem['id']}", {
        "resend_count": sent_count,
        "last_sent_at": datetime.now(timezone.utc).isoformat(),
    })


async def mark_done(reminder_id: str) -> date:
    """Handle the ✅ Done button: schedule the next cycle from today."""
    db = get_db()
    done_on = datetime.now(timezone.utc).date()
    next_due = add_three_and_half_months(done_on)
    rows = await db.select("reminders", f"id=eq.{reminder_id}")
    history = (rows[0].get("history") or []) if rows else []
    history.append({"done_at": done_on.isoformat()})
    await db.update("reminders", f"id=eq.{reminder_id}", {
        "next_due": next_due.isoformat(),
        "resend_count": 0,
        "last_sent_at": None,
        "overdue": False,
        "history": history,
    })
    return next_due
