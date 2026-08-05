"""Background asyncio watcher.

Runs two loops:
- housekeeping tick (reminders + optional hourly mock signal),
- the market service (Deriv WS candles D1/H4/H1/M15 × watchlist → SNR
  engine → Supabase). Phase C adds the Direction/Setup state machines,
  Phase D the tick-watch for armed virtual orders.

Statelessness rule: nothing decision-relevant lives only in memory — SNR
levels, state machines, virtual orders and reminders are persisted to
Supabase and reloaded on boot.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from app.services import reminders, signals
from app.services.market import get_market
from app.services.strategy import get_strategy
from app.services.supabase import get_db

log = logging.getLogger("maverick.watcher")

TICK_SECONDS = 60
MOCK_SIGNAL_INTERVAL = 3600  # one mock card per hour while enabled

_state = {"started_at": None, "last_tick": None, "ticks": 0, "last_error": None}


def status() -> dict:
    from app.execution.emulated_pending import get_execution
    return {
        **_state,
        "market": get_market().status(),
        "strategy": get_strategy().status(),
        "execution": get_execution().status(),
    }


async def run() -> None:
    from app.execution.emulated_pending import get_execution
    _state["started_at"] = datetime.now(UTC).isoformat()
    log.info("watcher started")
    from app.services.analytics import get_tracker
    market, strategy, execution = get_market(), get_strategy(), get_execution()
    market.strategy = strategy
    market.execution = execution
    market.analytics = get_tracker()
    strategy.market = market
    execution.market = market

    async def market_supervisor() -> None:
        """market.run() reconnects the WS itself, but a failure during its
        boot sequence (Supabase load on a cold start) would otherwise kill
        the market silently. Restart it with capped backoff."""
        backoff = 5
        while True:
            try:
                await market.run()
                return  # clean return = DERIV_APP_ID unset → stay idle
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market service crashed; restarting in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    market_task = asyncio.create_task(market_supervisor(), name="market")
    try:
        await _housekeeping()
    finally:
        market_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await market_task


async def _housekeeping() -> None:
    # Anchor the mock timer to boot time: monotonic clocks start at an
    # arbitrary (often huge) value, so `last_mock = 0` would fire a mock
    # card on every Render wake-up.
    last_mock = asyncio.get_event_loop().time()
    while True:
        try:
            _state["ticks"] += 1
            _state["last_tick"] = datetime.now(UTC).isoformat()

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
