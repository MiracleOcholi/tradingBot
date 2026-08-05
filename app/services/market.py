"""Market service — glues the Deriv candle streams to the SNR engine and
Supabase persistence, and serves chart data to the dashboard.

Statelessness: SNR levels live in `snr_levels`; the per-stream ingest cursor
lives in `engine_state.state_payload.snr_cursor[tf]`. On boot we reload both,
then Deriv history replay only processes candles newer than the cursor —
so Render sleeps/redeploys never double-count or skip candles.

SNR tracking runs on D1 and H4 (the timeframes the machine spec consumes
levels from). M15/H1 candles are cached for charts and for the Phase C
swing/engulfing logic.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.core.models import Candle, Formation, SNRLevel
from app.core.snr import SNRTracker
from app.services.deriv import GRANULARITY, DerivClient
from app.services.supabase import SupabaseClient, get_db

log = logging.getLogger("maverick.market")

SNR_TIMEFRAMES = ("D1", "H4")


def _level_to_row(level: SNRLevel) -> dict:
    return {
        "symbol": level.symbol,
        "timeframe": level.timeframe,
        "price": level.price,
        "formation": level.formation.value,
        "role": level.role,
        "break_count": level.break_count,
        "fresh": level.fresh,
        "touches": level.touches,
        "played": level.played,
        "first_candle_at": level.first_candle_at.isoformat(),
        "active": level.active,
    }


def _row_to_level(row: dict) -> SNRLevel:
    return SNRLevel(
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        price=float(row["price"]),
        formation=Formation(row["formation"]),
        role=row["role"],
        first_candle_at=datetime.fromisoformat(row["first_candle_at"].replace("Z", "+00:00")),
        break_count=row["break_count"],
        fresh=row["fresh"],
        touches=row["touches"],
        played=row["played"],
        active=row["active"],
        id=row["id"],
    )


class MarketService:
    def __init__(self, symbols: list[str], db: SupabaseClient | None = None) -> None:
        self.symbols = symbols
        self.db = db or get_db()
        self.trackers: dict[tuple[str, str], SNRTracker] = {}
        self.cursor: dict[tuple[str, str], datetime] = {}
        self.deriv: DerivClient | None = None
        self.candles_processed = 0
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- boot
    async def load(self) -> None:
        """Reload levels + cursors from Supabase (called once on boot)."""
        for symbol in self.symbols:
            rows = await self.db.select(
                "snr_levels",
                f"symbol=eq.{symbol}&active=is.true&order=first_candle_at.asc",
            )
            by_tf: dict[str, list[SNRLevel]] = {tf: [] for tf in SNR_TIMEFRAMES}
            for row in rows:
                if row["timeframe"] in by_tf:
                    by_tf[row["timeframe"]].append(_row_to_level(row))
            for tf in SNR_TIMEFRAMES:
                self.trackers[(symbol, tf)] = SNRTracker(
                    symbol, tf, GRANULARITY[tf], levels=by_tf[tf]
                )

            state_rows = await self.db.select("engine_state", f"symbol=eq.{symbol}")
            payload = (state_rows[0].get("state_payload") or {}) if state_rows else {}
            for tf, iso in (payload.get("snr_cursor") or {}).items():
                self.cursor[(symbol, tf)] = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        log.info(
            "market loaded: %d trackers, %d levels, %d cursors",
            len(self.trackers),
            sum(len(t.levels) for t in self.trackers.values()),
            len(self.cursor),
        )

    async def run(self) -> None:
        settings = get_settings()
        if not settings.deriv_app_id:
            log.warning("DERIV_APP_ID not set — market service idle")
            return
        await self.load()
        self.deriv = DerivClient(settings.deriv_app_id, self.symbols, self.on_candle)
        await self.deriv.run()

    # ---------------------------------------------------------------- ingest
    async def on_candle(self, symbol: str, tf: str, candle: Candle, is_history: bool) -> None:
        if tf not in SNR_TIMEFRAMES:
            return  # M15/H1: chart cache only (held by DerivClient)

        key = (symbol, tf)
        cursor = self.cursor.get(key)
        if cursor is not None and candle.ts <= cursor:
            return  # already processed before a restart

        async with self._lock:
            tracker = self.trackers.setdefault(
                key, SNRTracker(symbol, tf, GRANULARITY[tf])
            )
            # The tracker needs the true previous candle after a cold boot:
            # the first replayed candle only primes prev_candle (no pairing
            # with a candle we never saw).
            upd = tracker.process_candle(candle)
            self.candles_processed += 1
            try:
                await self._persist(upd)
                self.cursor[key] = candle.ts
                await self._save_cursor(symbol)
            except Exception:
                log.exception("persist failed for %s %s @ %s", symbol, tf, candle.ts)

    async def _persist(self, upd) -> None:
        if upd.new_level is not None:
            rows = await self.db.upsert(
                "snr_levels",
                _level_to_row(upd.new_level),
                on_conflict="symbol,timeframe,first_candle_at,formation",
            )
            if rows:
                upd.new_level.id = rows[0]["id"]

        changed: dict[str, SNRLevel] = {}
        for level in [*upd.touched, *upd.flipped, *upd.deactivated]:
            if level.id:
                changed[level.id] = level
        for level_id, level in changed.items():
            await self.db.update(
                "snr_levels",
                f"id=eq.{level_id}",
                {
                    "role": level.role,
                    "break_count": level.break_count,
                    "fresh": level.fresh,
                    "touches": level.touches,
                    "active": level.active,
                },
            )

    async def _save_cursor(self, symbol: str) -> None:
        payload_cursor = {
            tf: self.cursor[(symbol, tf)].isoformat()
            for tf in SNR_TIMEFRAMES
            if (symbol, tf) in self.cursor
        }
        rows = await self.db.select("engine_state", f"symbol=eq.{symbol}")
        payload = (rows[0].get("state_payload") or {}) if rows else {}
        payload["snr_cursor"] = payload_cursor
        await self.db.update(
            "engine_state",
            f"symbol=eq.{symbol}",
            {"state_payload": payload, "updated_at": datetime.now(timezone.utc).isoformat()},
        )

    # ---------------------------------------------------------------- charts
    def chart_data(self, symbol: str, tf: str, limit: int = 200) -> dict:
        candles = self.deriv.candles(symbol, tf, limit) if self.deriv else []
        levels: list[dict] = []
        for snr_tf in SNR_TIMEFRAMES:
            tracker = self.trackers.get((symbol, snr_tf))
            if not tracker:
                continue
            for l in tracker.active_levels():
                levels.append({
                    "timeframe": snr_tf,
                    "price": l.price,
                    "role": l.role,
                    "formation": l.formation.value,
                    "fresh": l.fresh,
                    "break_count": l.break_count,
                    "played": l.played,
                })
        return {
            "symbol": symbol,
            "tf": tf,
            "candles": [
                {"t": int(c.ts.timestamp()), "o": c.open, "h": c.high, "l": c.low, "c": c.close}
                for c in candles
            ],
            "levels": levels,
            "deriv": self.deriv.status() if self.deriv else {"connected": False},
        }

    def status(self) -> dict:
        return {
            "deriv": self.deriv.status() if self.deriv else {"connected": False},
            "trackers": len(self.trackers),
            "levels": sum(len(t.active_levels()) for t in self.trackers.values()),
            "candles_processed": self.candles_processed,
        }


_market: MarketService | None = None


def get_market() -> MarketService:
    global _market
    if _market is None:
        from app.config import WATCHLIST
        _market = MarketService(WATCHLIST)
    return _market
