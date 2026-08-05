"""Strategy service — runs one DirectionEngine + SetupMachine per symbol on
live candle closes, persists their state to engine_state, and turns confirmed
setups into signals (Telegram card + signals row).

Statelessness: direction, setup_state and the pending-setup payload are
persisted on every change and rehydrated on boot. Candle history for swing /
engulfing math is rebuilt from the Deriv history replay after every restart.

Replay safety: signals confirmed from stale replayed candles (older than
STALE_SIGNAL_S) are logged as EXPIRED and never alerted or armed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.direction import DirectionCandidate, DirectionEngine, DirectionState
from app.core.models import Candle, SetupState
from app.core.setup_sm import PendingSetup, SetupMachine
from app.services.supabase import SupabaseClient, get_db

log = logging.getLogger("maverick.strategy")

STALE_SIGNAL_S = 1800  # 30 min


class SymbolStrategy:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.direction = DirectionEngine(symbol)
        self.setup = SetupMachine(symbol)

    # ------------------------------------------------------------ persistence
    def to_row_patch(self) -> dict:
        ds = self.direction.state
        payload: dict = {}
        if ds.candidate is not None:
            c = ds.candidate
            payload["direction_candidate"] = {
                "side": c.side,
                "level_id": c.level_id,
                "level_price": c.level_price,
                "level_role": c.level_role,
                "tap_ts": c.tap_ts.isoformat(),
            }
        if self.setup.pending is not None:
            payload["pending_setup"] = self.setup.pending.to_payload()
        return {
            "direction": ds.direction,
            "direction_since": ds.since.isoformat() if ds.since else None,
            "setup_state": self.setup.state.value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "_payload_patch": payload,  # merged into state_payload by the caller
        }

    def load_row(self, row: dict) -> None:
        payload = row.get("state_payload") or {}
        ds = DirectionState(direction=row.get("direction"))
        if row.get("direction_since"):
            ds.since = datetime.fromisoformat(row["direction_since"].replace("Z", "+00:00"))
        cand = payload.get("direction_candidate")
        if cand:
            ds.candidate = DirectionCandidate(
                side=cand["side"],
                level_id=cand.get("level_id"),
                level_price=cand["level_price"],
                level_role=cand["level_role"],
                tap_ts=datetime.fromisoformat(cand["tap_ts"]),
            )
        self.direction = DirectionEngine(self.symbol, ds)

        pending = payload.get("pending_setup")
        self.setup = SetupMachine(
            self.symbol,
            direction=ds.direction,
            state=SetupState(row.get("setup_state") or "IDLE"),
            pending=PendingSetup.from_payload(pending) if pending else None,
        )


class StrategyService:
    def __init__(self, symbols: list[str], db: SupabaseClient | None = None) -> None:
        self.symbols = symbols
        self.db = db or get_db()
        self.strategies = {s: SymbolStrategy(s) for s in symbols}
        self.market = None  # set by MarketService (avoids circular import)
        self.signals_confirmed = 0

    # ---------------------------------------------------------------- boot
    async def load(self) -> None:
        for symbol in self.symbols:
            rows = await self.db.select("engine_state", f"symbol=eq.{symbol}")
            if rows:
                try:
                    self.strategies[symbol].load_row(rows[0])
                except Exception:
                    log.exception("failed to rehydrate %s; starting clean", symbol)
        log.info("strategy loaded for %d symbols", len(self.strategies))

    # ------------------------------------------------------------ providers
    def _tracker(self, symbol: str, tf: str):
        return self.market.trackers.get((symbol, tf)) if self.market else None

    def _fresh_d1(self, symbol: str, role: str | None = None):
        tracker = self._tracker(symbol, "D1")
        if not tracker:
            return []
        return [
            l for l in tracker.active_levels()
            if l.fresh and not l.played and (role is None or l.role == role)
        ]

    def _candles(self, symbol: str, tf: str) -> list[Candle]:
        if not self.market or not self.market.deriv:
            return []
        return self.market.deriv.candles(symbol, tf, limit=400, completed_only=True)

    # ---------------------------------------------------------------- ingest
    async def on_candle(self, symbol: str, tf: str, candle: Candle, is_history: bool) -> None:
        strat = self.strategies.get(symbol)
        if strat is None:
            return
        dir_events: list = []
        setup_events: list = []

        if tf == "M15":
            dir_events = strat.direction.on_m15_close(candle, self._fresh_d1(symbol))
            setup_events = strat.setup.on_m15_close(
                candle, self._candles(symbol, "M15"), self._fresh_d1(symbol, strat.setup.opposite_role)
            )
        elif tf == "H1":
            setup_events = strat.setup.on_h1_close(
                candle, self._candles(symbol, "H1"), self._candles(symbol, "M15")
            )
        elif tf == "H4":
            tracker = self._tracker(symbol, "H4")
            provider = (
                (lambda role, before: tracker.last_traditional(role, before))
                if tracker else (lambda role, before: None)
            )
            dir_events = strat.direction.on_h4_close(candle, provider)
            setup_events = strat.setup.on_h4_close(candle)
        elif tf == "D1":
            dir_events = strat.direction.on_d1_close(candle)
            setup_events = strat.setup.on_d1_close(candle)

        for ev in dir_events:
            if ev.kind in ("CONFIRMED", "FLIPPED"):
                log.info("%s direction %s: %s", symbol, ev.kind, ev.data)
                setup_events += strat.setup.set_direction(strat.direction.state.direction)
            elif ev.kind == "TAP":
                await self._defresh_level(symbol, "tap", strat.direction.state.candidate.level_id)

        for ev in setup_events:
            if ev.kind == "TAPPED":
                pend = strat.setup.pending
                if pend:
                    await self._defresh_level(symbol, "tap", pend.level_id)
            elif ev.kind == "INVALIDATED":
                log.info("%s setup invalidated: %s", symbol, ev.data.get("reason"))
            elif ev.kind == "CONFIRMED":
                await self._emit_signal(symbol, strat, ev.data, candle, is_history)

        if dir_events or setup_events:
            await self._persist(symbol, strat)

    # ---------------------------------------------------------------- effects
    async def _defresh_level(self, symbol: str, why: str, level_id: str | None) -> None:
        """First arrival at a level: it is tested from now on (kept in sync in
        both the tracker object and the DB, without waiting for the D1 close)."""
        if not level_id:
            return
        tracker = self._tracker(symbol, "D1")
        if tracker:
            for l in tracker.levels:
                if l.id == level_id and l.touches == 0:
                    l.touches = 1
                    l.fresh = False
        try:
            await self.db.update(
                "snr_levels", f"id=eq.{level_id}&touches=eq.0",
                {"touches": 1, "fresh": False},
            )
        except Exception:
            log.exception("defresh(%s) failed for %s", why, level_id)

    async def _mark_played(self, symbol: str, level_id: str | None) -> None:
        if not level_id:
            return
        tracker = self._tracker(symbol, "D1")
        if tracker:
            for l in tracker.levels:
                if l.id == level_id:
                    l.played = True
                    l.fresh = False
        try:
            await self.db.update(
                "snr_levels", f"id=eq.{level_id}", {"played": True, "fresh": False}
            )
        except Exception:
            log.exception("mark_played failed for %s", level_id)

    async def _emit_signal(
        self, symbol: str, strat: SymbolStrategy, data: dict, candle: Candle, is_history: bool
    ) -> None:
        from app.services import signals as signal_svc

        plan = data["plan"]
        await self._mark_played(symbol, data.get("level_id"))
        age_s = (datetime.now(timezone.utc) - candle.ts).total_seconds()
        stale = is_history and age_s > STALE_SIGNAL_S
        context = {
            "direction": strat.direction.state.direction,
            "daily_snr": data.get("level_price"),
            "engulf_type": data.get("engulf_type"),
            "tap_ts": data.get("tap_ts"),
            "stale_replay": stale,
        }
        self.signals_confirmed += 1
        await signal_svc.create_setup_signal(
            symbol=symbol,
            side=plan.side,
            entry=plan.entry,
            sl=plan.sl,
            tp=plan.tp,
            order_block=data.get("order_block"),
            context=context,
            expired=stale,
        )

    async def _persist(self, symbol: str, strat: SymbolStrategy) -> None:
        patch = strat.to_row_patch()
        payload_patch = patch.pop("_payload_patch")
        try:
            rows = await self.db.select("engine_state", f"symbol=eq.{symbol}")
            payload = (rows[0].get("state_payload") or {}) if rows else {}
            payload.pop("direction_candidate", None)
            payload.pop("pending_setup", None)
            payload.update(payload_patch)
            await self.db.update(
                "engine_state", f"symbol=eq.{symbol}", {**patch, "state_payload": payload}
            )
        except Exception:
            log.exception("persist engine_state failed for %s", symbol)

    def status(self) -> dict:
        return {
            s: {
                "direction": st.direction.state.direction,
                "setup_state": st.setup.state.value,
            }
            for s, st in self.strategies.items()
        } | {"_signals_confirmed": self.signals_confirmed}


_strategy: StrategyService | None = None


def get_strategy() -> StrategyService:
    global _strategy
    if _strategy is None:
        from app.config import WATCHLIST
        _strategy = StrategyService(WATCHLIST)
    return _strategy
