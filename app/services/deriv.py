"""Deriv WebSocket client — one connection, candle streams for all symbols.

Phase B scope: public market data only (ticks_history style=candles with
subscribe) — no authorization needed for synthetic-index candles. Phase D
adds authorize + buy on the same connection.

Design:
- Single WS to wss://ws.derivws.com/websockets/v3?app_id=<DERIV_APP_ID>.
- 7 symbols × 4 granularities (M15/H1/H4/D1) = 28 candle subscriptions.
- A candle is COMPLETED when an ohlc update arrives with a NEWER open_time;
  the previously forming candle is then final and fed to `on_candle`.
- Auto-reconnect with capped exponential backoff + full resubscribe.
- In-memory rolling history per stream (charts + engine warm-up).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

import websockets

from app.core.models import Candle

log = logging.getLogger("maverick.deriv")

GRANULARITY = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
TF_OF_GRANULARITY = {v: k for k, v in GRANULARITY.items()}

# on_candle(symbol, timeframe, candle, is_history)
CandleHandler = Callable[[str, str, Candle, bool], Awaitable[None]]


def _to_candle(d: dict) -> Candle:
    return Candle(
        ts=datetime.fromtimestamp(int(d["epoch"] if "epoch" in d else d["open_time"]), tz=timezone.utc),
        open=float(d["open"]),
        high=float(d["high"]),
        low=float(d["low"]),
        close=float(d["close"]),
    )


class CandleStream:
    """Book-keeping for one (symbol, timeframe) subscription."""

    def __init__(self, symbol: str, timeframe: str, maxlen: int = 400) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.completed: deque[Candle] = deque(maxlen=maxlen)
        self.forming: Candle | None = None

    def ingest_history(self, candles: list[dict]) -> list[Candle]:
        """Initial `candles` payload: all but the last are completed; the last
        is the currently forming candle."""
        parsed = [_to_candle(c) for c in candles]
        if not parsed:
            return []
        done, self.forming = parsed[:-1], parsed[-1]
        self.completed.extend(done)
        return done

    def ingest_ohlc(self, ohlc: dict) -> Candle | None:
        """Streaming update. Returns the just-COMPLETED candle, if any."""
        open_time = datetime.fromtimestamp(int(ohlc["open_time"]), tz=timezone.utc)
        candle = Candle(
            ts=open_time,
            open=float(ohlc["open"]),
            high=float(ohlc["high"]),
            low=float(ohlc["low"]),
            close=float(ohlc["close"]),
        )
        finished: Candle | None = None
        if self.forming is not None and open_time > self.forming.ts:
            finished = self.forming
            self.completed.append(finished)
        self.forming = candle
        return finished


class DerivClient:
    def __init__(
        self,
        app_id: str,
        symbols: list[str],
        on_candle: CandleHandler,
        timeframes: list[str] | None = None,
        history_count: int = 300,
    ) -> None:
        self.url = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
        self.symbols = symbols
        self.timeframes = timeframes or list(GRANULARITY)
        self.on_candle = on_candle
        self.history_count = history_count
        self.streams: dict[tuple[str, str], CandleStream] = {}
        self.connected = False
        self.last_message_at: datetime | None = None
        self._req_id = 0

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "streams": len(self.streams),
        }

    def candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        stream = self.streams.get((symbol, timeframe))
        if not stream:
            return []
        out = list(stream.completed)[-limit:]
        if stream.forming:
            out.append(stream.forming)  # forming candle last (chart draws it lighter)
        return out

    # ------------------------------------------------------------------
    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("deriv ws dropped (%r); reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session(self) -> None:
        async with websockets.connect(self.url, ping_interval=30, ping_timeout=15) as ws:
            self.connected = True
            log.info("deriv ws connected")
            try:
                for symbol in self.symbols:
                    for tf in self.timeframes:
                        self._req_id += 1
                        await ws.send(json.dumps({
                            "ticks_history": symbol,
                            "adjust_start_time": 1,
                            "count": self.history_count,
                            "end": "latest",
                            "granularity": GRANULARITY[tf],
                            "style": "candles",
                            "subscribe": 1,
                            "req_id": self._req_id,
                            "passthrough": {"symbol": symbol, "tf": tf},
                        }))
                async for raw in ws:
                    self.last_message_at = datetime.now(timezone.utc)
                    await self._handle(json.loads(raw))
            finally:
                self.connected = False
                log.info("deriv ws disconnected")

    async def _handle(self, msg: dict) -> None:
        if msg.get("error"):
            log.error("deriv error: %s", msg["error"])
            return

        msg_type = msg.get("msg_type")
        if msg_type == "candles":
            pt = msg.get("echo_req", {}).get("passthrough", {})
            symbol, tf = pt.get("symbol"), pt.get("tf")
            if not symbol or not tf:
                return
            stream = self.streams.setdefault((symbol, tf), CandleStream(symbol, tf))
            for candle in stream.ingest_history(msg.get("candles", [])):
                await self.on_candle(symbol, tf, candle, True)

        elif msg_type == "ohlc":
            ohlc = msg["ohlc"]
            symbol = ohlc["symbol"]
            tf = TF_OF_GRANULARITY.get(int(ohlc["granularity"]))
            if tf is None:
                return
            stream = self.streams.setdefault((symbol, tf), CandleStream(symbol, tf))
            finished = stream.ingest_ohlc(ohlc)
            if finished is not None:
                await self.on_candle(symbol, tf, finished, False)
