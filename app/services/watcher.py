"""Background asyncio watcher.

Phase A: heartbeat + reminder-engine ticks (+ hourly mock signal when
config.mock_signals is on) — proves the loop survives Koyeb sleep/wake.
Phase B adds: Deriv WS candles (D1/H4/H1/M15 × 7 symbols) → SNR engine.
Phase C adds: Direction + Setup state machines.
Phase D adds: tick-watch for armed virtual orders → multiplier execution.

Statelessness rule: nothing decision-relevant lives only in memory — SNR
levels, state machines, virtual orders and reminders are persisted to
Supabase and reloaded on boot.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.services import reminders, signals
from app.services.supabase import get_db

log = logging.getLogger("maverick.watcher")

TICK_SECONDS = 60
MOCK_SIGNAL_INTERVAL = 3600  # one mock card per hour while enabled

_state = {"started_at": None, "last_tick": None, "ticks": 0, "last_error": None}


def status() -> dict:
    return dict(_state)


async def run() -> None:
    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    log.info("watcher started")
    last_mock = 0.0
    while True:
        try:
            _state["ticks"] += 1
            _state["last_tick"] = datetime.now(timezone.utc).isoformat()

            await reminders.tick()

            cfg = await get_db().get_config()
            now = asyncio.get_event_loop().time()
            if cfg.get("mock_signals") and now - last_mock >= MOCK_SIGNAL_INTERVAL:
                last_mock = now
                await signals.create_mock_signal()

            _state["last_error"] = None
        except asyncio.CancelledError:
            log.info("watcher cancelled")
            raise
        except Exception as e:  # keep the loop alive through transient failures
            _state["last_error"] = repr(e)
            log.exception("watcher tick failed")
        await asyncio.sleep(TICK_SECONDS)
