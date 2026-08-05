"""A pool of Deriv sockets, because one socket cannot carry the watchlist.

The current API silently accepts only the first 8 subscriptions on a
connection: with 6 symbols × 4 timeframes = 24 requested, exactly 8 arrived
(R_10 and R_50 complete, everything else empty) and NO error was returned.
Nothing in the protocol reports the limit, so it has to be designed around.

Symbols are therefore sharded across several connections, each carrying at
most `max_subscriptions` of them. Every connection mints its own URL — the
handshake is per-socket — and reconnects independently, so one dropped
shard never takes the others down.

This class is a drop-in facade for DerivClient: candle and tick calls route
to the shard owning the symbol, while trading calls (authorize / send /
contract watching) go to a designated primary shard, since those are
account-scoped rather than symbol-scoped.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.core.models import Candle
from app.services.deriv import GRANULARITY, PUBLIC_FALLBACK_APP_ID, DerivClient

log = logging.getLogger("maverick.deriv.pool")

# Observed ceiling on the live API. Kept a little under it so a shard is
# never sitting exactly on the boundary.
MAX_SUBSCRIPTIONS_PER_CONNECTION = 8


def shard_symbols(symbols: list[str], per_shard: int) -> list[list[str]]:
    per_shard = max(1, per_shard)
    return [symbols[i:i + per_shard] for i in range(0, len(symbols), per_shard)]


class DerivPool:
    def __init__(
        self,
        app_id: str,
        symbols: list[str],
        on_candle,
        timeframes: list[str] | None = None,
        history_count: int = 300,
        on_history_done=None,
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
        fallback_app_id: str | None = PUBLIC_FALLBACK_APP_ID,
        url_provider: Callable[[], Awaitable[str | None]] | None = None,
        max_subscriptions: int = MAX_SUBSCRIPTIONS_PER_CONNECTION,
    ) -> None:
        self.timeframes = timeframes or list(GRANULARITY)
        per_shard = max(1, max_subscriptions // max(1, len(self.timeframes)))
        self.shards = shard_symbols(symbols, per_shard)
        self.clients: list[DerivClient] = [
            DerivClient(
                app_id, shard, on_candle,
                timeframes=self.timeframes,
                history_count=history_count,
                on_history_done=on_history_done,
                token_provider=token_provider,
                fallback_app_id=fallback_app_id,
                url_provider=url_provider,
            )
            for shard in self.shards
        ]
        self._owner: dict[str, DerivClient] = {
            symbol: client
            for client, shard in zip(self.clients, self.shards, strict=True)
            for symbol in shard
        }
        log.info("deriv pool: %d symbols across %d connections (%d subs each)",
                 len(symbols), len(self.clients), per_shard * len(self.timeframes))

    # ---- lifecycle --------------------------------------------------------
    async def run(self) -> None:
        """Run every shard. A shard that dies is restarted by its own
        reconnect loop; if one raises out entirely the rest keep running."""
        await asyncio.gather(*(c.run() for c in self.clients), return_exceptions=True)

    # ---- callbacks propagate to every shard -------------------------------
    @property
    def on_tick(self):
        return self.clients[0].on_tick if self.clients else None

    @on_tick.setter
    def on_tick(self, fn) -> None:
        for c in self.clients:
            c.on_tick = fn

    @property
    def on_contract(self):
        return self.clients[0].on_contract if self.clients else None

    @on_contract.setter
    def on_contract(self, fn) -> None:
        for c in self.clients:
            c.on_contract = fn

    # ---- symbol-scoped routing -------------------------------------------
    @property
    def primary(self) -> DerivClient:
        """Shard used for account-scoped calls (authorize, buy, balance)."""
        return self.clients[0]

    def _client_for(self, symbol: str) -> DerivClient:
        return self._owner.get(symbol, self.primary)

    def candles(self, symbol: str, timeframe: str, limit: int = 200,
                completed_only: bool = False) -> list[Candle]:
        return self._client_for(symbol).candles(symbol, timeframe, limit, completed_only)

    async def subscribe_ticks(self, symbol: str) -> None:
        await self._client_for(symbol).subscribe_ticks(symbol)

    async def unsubscribe_ticks(self, symbol: str) -> None:
        await self._client_for(symbol).unsubscribe_ticks(symbol)

    # ---- account-scoped calls go to the primary shard ---------------------
    async def send(self, payload: dict, timeout: float = 20.0) -> dict:
        return await self.primary.send(payload, timeout)

    async def authorize(self, token: str) -> dict:
        return await self.primary.authorize(token)

    async def watch_contract(self, contract_id: int) -> None:
        await self.primary.watch_contract(contract_id)

    def unwatch_contract(self, contract_id: int) -> None:
        self.primary.unwatch_contract(contract_id)

    # ---- aggregated view ---------------------------------------------------
    def status(self) -> dict:
        per = [c.status() for c in self.clients]
        errors: list[dict] = []
        for s in per:
            errors.extend(s.get("api_errors") or [])
        first = per[0] if per else {}
        return {
            "connected": any(s.get("connected") for s in per),
            "connections": len(per),
            "connections_up": sum(1 for s in per if s.get("connected")),
            "streams": sum(s.get("streams", 0) for s in per),
            "subscribed": sum(s.get("subscribed", 0) for s in per),
            "endpoint": first.get("endpoint"),
            "active_app_id": first.get("active_app_id"),
            "using_fallback_app_id": first.get("using_fallback_app_id"),
            "auth": first.get("auth"),
            "symbols_probe": first.get("symbols_probe"),
            "skipped_symbols": sorted({
                sym for s in per for sym in (s.get("skipped_symbols") or [])
            }),
            "available_synthetics": first.get("available_synthetics"),
            "last_error": next((s.get("last_error") for s in per if s.get("last_error")), None),
            "api_errors": errors[-30:],
            "shards": [
                {"symbols": shard, "connected": s.get("connected"), "streams": s.get("streams")}
                for shard, s in zip(self.shards, per, strict=True)
            ],
        }
