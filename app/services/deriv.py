"""Deriv WebSocket client — ONE connection for everything.

- Candle streams (public): 7 symbols × M15/H1/H4/D1; a candle is COMPLETED
  when an ohlc update arrives with a newer open_time.
- Tick streams: subscribed on demand while virtual pending orders are armed.
- Trading (Phase D): request/response correlation by req_id (authorize,
  balance, contracts_for, proposal, buy) and proposal_open_contract
  subscriptions for open-trade outcomes.
- Auto-reconnect with capped exponential backoff; candle + tick + contract
  subscriptions are all re-established; authorization is re-applied by the
  execution layer per account before trading calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import websockets

from app.core.models import Candle

log = logging.getLogger("maverick.deriv")


class DerivAPIError(Exception):
    def __init__(self, error: dict) -> None:
        self.code = error.get("code", "")
        self.message = error.get("message", "")
        super().__init__(f"{self.code}: {self.message}")


GRANULARITY = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
TF_OF_GRANULARITY = {v: k for k, v in GRANULARITY.items()}

# on_candle(symbol, timeframe, candle, is_history)
CandleHandler = Callable[[str, str, Candle, bool], Awaitable[None]]


def _to_candle(d: dict) -> Candle:
    return Candle(
        ts=datetime.fromtimestamp(int(d["epoch"] if "epoch" in d else d["open_time"]), tz=UTC),
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
        is the currently forming candle.

        On RECONNECT the same stream object receives an overlapping batch —
        only candles newer than what we already hold are appended, keeping
        the cache strictly chronological (the swing/engulfing logic relies
        on adjacency)."""
        parsed = [_to_candle(c) for c in candles]
        if not parsed:
            return []
        done, self.forming = parsed[:-1], parsed[-1]
        last = self.completed[-1].ts if self.completed else None
        fresh = [c for c in done if last is None or c.ts > last]
        self.completed.extend(fresh)
        return fresh

    def ingest_ohlc(self, ohlc: dict) -> Candle | None:
        """Streaming update. Returns the just-COMPLETED candle, if any."""
        open_time = datetime.fromtimestamp(int(ohlc["open_time"]), tz=UTC)
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
        on_history_done: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.url = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
        self.symbols = symbols
        self.timeframes = timeframes or list(GRANULARITY)
        self.on_candle = on_candle
        self.on_history_done = on_history_done
        self.history_count = history_count
        self.streams: dict[tuple[str, str], CandleStream] = {}
        self.connected = False
        self.last_message_at: datetime | None = None
        self._req_id = 0
        self._ws = None
        self._pending: dict[int, asyncio.Future] = {}
        self._tick_symbols: set[str] = set()
        self._contract_ids: set[int] = set()
        self.on_tick = None       # async (symbol, quote: float, epoch: int)
        self.on_contract = None   # async (contract: dict)
        self.authorized_loginid: str | None = None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "streams": len(self.streams),
        }

    def candles(
        self, symbol: str, timeframe: str, limit: int = 200, completed_only: bool = False
    ) -> list[Candle]:
        stream = self.streams.get((symbol, timeframe))
        if not stream:
            return []
        out = list(stream.completed)[-limit:]
        if stream.forming and not completed_only:
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
            self._ws = ws
            self.connected = True
            self.authorized_loginid = None  # a new socket is unauthorized
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
                for symbol in list(self._tick_symbols):
                    await ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                for cid in list(self._contract_ids):
                    await ws.send(json.dumps(
                        {"proposal_open_contract": 1, "contract_id": cid, "subscribe": 1}
                    ))
                async for raw in ws:
                    self.last_message_at = datetime.now(UTC)
                    await self._handle(json.loads(raw))
            finally:
                self.connected = False
                self._ws = None
                self.authorized_loginid = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("deriv ws dropped"))
                self._pending.clear()
                log.info("deriv ws disconnected")

    # ------------------------------------------------------------ trading API
    async def send(self, payload: dict, timeout: float = 20.0) -> dict:
        """Send a request and await its correlated response. Raises on Deriv
        error responses (DerivAPIError) and on connection loss."""
        if self._ws is None:
            raise ConnectionError("deriv ws not connected")
        self._req_id += 1
        req_id = self._req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({**payload, "req_id": req_id}))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def authorize(self, token: str) -> dict:
        resp = await self.send({"authorize": token})
        auth = resp.get("authorize", {})
        self.authorized_loginid = auth.get("loginid")
        return auth

    async def subscribe_ticks(self, symbol: str) -> None:
        if symbol in self._tick_symbols:
            return
        self._tick_symbols.add(symbol)
        if self._ws is not None:
            await self._ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))

    async def unsubscribe_ticks(self, symbol: str) -> None:
        """Deriv's forget needs per-subscription ids; the simple reliable move
        is forget_all("ticks") once nothing remains armed, and resubscribing
        the survivors otherwise."""
        self._tick_symbols.discard(symbol)
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"forget_all": "ticks"}))
        for s in list(self._tick_symbols):
            await self._ws.send(json.dumps({"ticks": s, "subscribe": 1}))

    async def watch_contract(self, contract_id: int) -> None:
        self._contract_ids.add(contract_id)
        if self._ws is not None:
            await self._ws.send(json.dumps(
                {"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}
            ))

    def unwatch_contract(self, contract_id: int) -> None:
        self._contract_ids.discard(contract_id)

    async def _handle(self, msg: dict) -> None:
        # Correlated request/response first (trading calls, authorize, …).
        req_id = msg.get("req_id")
        if req_id is not None and req_id in self._pending:
            fut = self._pending[req_id]
            if not fut.done():
                if msg.get("error"):
                    fut.set_exception(DerivAPIError(msg["error"]))
                else:
                    fut.set_result(msg)
            # fall through: subscription confirmations also carry stream data

        if msg.get("error"):
            if req_id is None:
                log.error("deriv error: %s", msg["error"])
            return

        msg_type = msg.get("msg_type")
        if msg_type == "tick" and self.on_tick is not None:
            tick = msg["tick"]
            await self.on_tick(tick["symbol"], float(tick["quote"]), int(tick["epoch"]))
            return
        if msg_type == "proposal_open_contract" and self.on_contract is not None:
            await self.on_contract(msg["proposal_open_contract"])
            return
        if msg_type == "candles":
            pt = msg.get("echo_req", {}).get("passthrough", {})
            symbol, tf = pt.get("symbol"), pt.get("tf")
            if not symbol or not tf:
                return
            stream = self.streams.setdefault((symbol, tf), CandleStream(symbol, tf))
            for candle in stream.ingest_history(msg.get("candles", [])):
                await self.on_candle(symbol, tf, candle, True)
            if self.on_history_done is not None:
                await self.on_history_done(symbol, tf)

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
