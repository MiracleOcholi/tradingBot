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
SUBSCRIBE_STAGGER_S = 0.35   # spread the initial subscription burst

# Deriv's public documentation app id. Candle data is public, so when the
# configured app id is refused by this endpoint (the current dashboard
# issues ids for the newer REST/WS API, which the legacy socket rejects
# with HTTP 401) the data socket falls back to this rather than going dark.
PUBLIC_FALLBACK_APP_ID = "1089"
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
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
        fallback_app_id: str | None = PUBLIC_FALLBACK_APP_ID,
        url_provider: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self.token_provider = token_provider
        # Resolved before EVERY connection attempt: the current-generation
        # endpoint is opened with a single-use OTP, so a reconnect needs a
        # freshly minted URL rather than the previous one.
        self.url_provider = url_provider
        self.resolved_url: str | None = None
        self.app_id = app_id
        self.fallback_app_id = fallback_app_id or None
        self.active_app_id = app_id
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
        self.last_error: str | None = None
        self.connect_attempts = 0
        self.recent_errors: deque[dict] = deque(maxlen=30)
        # Kept out of the rotating buffer: the authorize outcome is the fact
        # that explains an empty symbol list, and it must not be pushed out
        # by a burst of per-symbol errors that follow it.
        self.auth_state: dict = {"attempted": False}
        self.symbols_probe: dict = {}
        self.subscriptions_sent = 0
        self.skipped_symbols: list[str] = []
        self.available_synthetics: list[str] = []

    @property
    def url(self) -> str:
        return f"wss://ws.derivws.com/websockets/v3?app_id={self.active_app_id}"

    async def resolve_url(self) -> str:
        """Current-generation URL when one can be minted, else legacy."""
        if self.url_provider is not None:
            try:
                url = await self.url_provider()
            except Exception:
                log.exception("websocket url provider failed; using the legacy endpoint")
                url = None
            if url:
                self.resolved_url = url
                return url
        self.resolved_url = None
        return self.url

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "active_app_id": self.active_app_id,
            "using_fallback_app_id": self.active_app_id != self.app_id,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "streams": len(self.streams),
            "connect_attempts": self.connect_attempts,
            # Surfaced so a failing socket is diagnosable from /health alone,
            # without shell access to the Render logs.
            "last_error": self.last_error,
            "api_errors": list(self.recent_errors),
            "subscribed": self.subscriptions_sent,
            # Configured codes Deriv does not recognise, and the real
            # synthetic-index codes it does offer — so a wrong code names
            # its own replacement.
            "skipped_symbols": self.skipped_symbols,
            "available_synthetics": self.available_synthetics,
            "auth": self.auth_state,
            "symbols_probe": self.symbols_probe,
            "endpoint": (
                "current-api" if self.resolved_url and "trading/v1" in self.resolved_url
                else "legacy"
            ),
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
                self.connect_attempts += 1
                await self._session()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                rejected = "401" in str(e) or "403" in str(e)
                if rejected and self._switch_to_fallback():
                    # Candle data is public. Rather than sit at zero because
                    # this endpoint will not accept the configured app id,
                    # carry on with the public one and say so loudly.
                    detail += (
                        f" — app id {self.app_id!r} refused by the legacy WebSocket "
                        f"endpoint; retrying with the public app id "
                        f"{self.active_app_id!r} for market data."
                    )
                    self.last_error = detail
                    log.error(detail)
                    backoff = 1
                    await asyncio.sleep(backoff)
                    continue
                if rejected:
                    detail += (
                        " — Deriv rejected the app id for the legacy WebSocket "
                        "endpoint. Identifiers issued by the current dashboard are "
                        "for the newer REST/WS API and are not interchangeable here."
                    )
                self.last_error = detail
                log.warning("deriv ws dropped (%s); reconnecting in %ss", detail, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _switch_to_fallback(self) -> bool:
        """Move the socket onto the public app id. Returns False if there is
        nothing to switch to, or we are already on it."""
        if not self.fallback_app_id:
            return False
        if self.active_app_id == self.fallback_app_id:
            return False
        self.active_app_id = self.fallback_app_id
        return True

    async def _session(self) -> None:
        url = await self.resolve_url()
        async with websockets.connect(url, ping_interval=30, ping_timeout=15) as ws:
            self._ws = ws
            self.connected = True
            self.last_error = None
            self.authorized_loginid = None  # a new socket is unauthorized
            log.info("deriv ws connected")
            # The reader must run before setup: the active-symbols probe is a
            # correlated request and would deadlock waiting on a loop that
            # had not started yet.
            reader = asyncio.create_task(self._read_loop(ws), name="deriv-reader")
            try:
                await self._bootstrap(ws)
                await reader
            finally:
                reader.cancel()
                self.connected = False
                self._ws = None
                self.authorized_loginid = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("deriv ws dropped"))
                self._pending.clear()
                log.info("deriv ws disconnected")

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            self.last_message_at = datetime.now(UTC)
            await self._handle(json.loads(raw))

    async def resolve_symbols(self) -> list[str]:
        """Ask Deriv which of the configured symbols actually exist.

        Symbol codes drift (and differ by app/landing company). Subscribing
        blindly meant one bad code produced an InvalidSymbol error that was
        indistinguishable from a dead feed. We now check first, stream what
        is valid, and report the rest — with the real codes Deriv offers, so
        a wrong one is trivially correctable.
        """
        try:
            # Exactly the request Deriv answers with a full list. Adding
            # `product_type: "basic"` came back with zero rows on the live
            # socket, so it is deliberately omitted.
            resp = await self.send({"active_symbols": "brief"})
        except Exception as e:
            log.warning("active_symbols probe failed (%s); subscribing optimistically", e)
            self.symbols_probe = {"error": str(e)}
            self.available_synthetics = []
            self.skipped_symbols = []
            return list(self.symbols)

        rows = resp.get("active_symbols", []) or []
        self.symbols_probe = {
            "rows": len(rows),
            "sample": rows[0] if rows else None,
        }
        # Deriv returns `underlying_symbol` / `underlying_symbol_name`;
        # older payloads used `symbol` / `display_name`. Accept both.
        def code(r: dict) -> str | None:
            return r.get("underlying_symbol") or r.get("symbol")

        def name(r: dict) -> str:
            return r.get("underlying_symbol_name") or r.get("display_name") or ""

        available = {code(r) for r in rows if code(r)}
        self.available_synthetics = sorted(
            f"{code(r)} ({name(r)})"
            for r in rows
            if str(r.get("market", "")).startswith("synthetic") and code(r)
        )
        if not available:
            # Empty or unparseable list: subscribing optimistically at least
            # yields per-symbol errors. Skipping everything yields silence,
            # and silence is what we are trying to eliminate.
            log.error(
                "active_symbols gave no usable codes (%d rows) — subscribing "
                "optimistically; sample row: %s", len(rows), rows[0] if rows else None,
            )
            self.skipped_symbols = []
            return list(self.symbols)
        valid = [s for s in self.symbols if s in available]
        self.skipped_symbols = [s for s in self.symbols if s not in available]
        if self.skipped_symbols:
            log.error(
                "these configured symbols do not exist on Deriv and were skipped: %s",
                ", ".join(self.skipped_symbols),
            )
        log.info("subscribing to %d/%d symbols", len(valid), len(self.symbols))
        return valid

    async def _authorize_if_possible(self) -> None:
        """Authorize the data socket when a token is available.

        An unauthorized connection only sees the instruments the app's
        default landing company offers — which came back EMPTY for the
        public app id. Authorizing resolves the connection to the real
        account, so active_symbols reflects what that account can trade.
        """
        self.auth_state = {"attempted": True}
        if self.token_provider is None:
            self.auth_state = {"attempted": False, "reason": "no token provider"}
            return
        try:
            token = await self.token_provider()
        except Exception as e:
            self.auth_state = {"attempted": False, "reason": f"token lookup failed: {e}"}
            log.exception("token lookup failed; continuing unauthorized")
            return
        if not token:
            self.auth_state = {"attempted": False, "reason": "no Deriv token stored"}
            log.info("no Deriv token stored; market socket stays unauthorized")
            return
        try:
            auth = await self.authorize(token)
            self.auth_state = {
                "attempted": True, "ok": True,
                "loginid": auth.get("loginid"),
                "is_virtual": bool(auth.get("is_virtual")),
                "landing_company": auth.get("landing_company_name"),
                "currency": auth.get("currency"),
            }
            log.info("market socket authorized as %s (%s)",
                     auth.get("loginid"), auth.get("landing_company_name"))
        except Exception as e:
            self.auth_state = {"attempted": True, "ok": False, "error": str(e)}
            # Bad/expired token must not stop market data being attempted.
            self.recent_errors.append({
                "code": "AuthorizeFailed", "message": str(e),
                "request": {"authorize": "<token>"},
                "at": datetime.now(UTC).isoformat(),
            })
            log.error("market socket authorize failed (%s); continuing unauthorized", e)

    async def _bootstrap(self, ws) -> None:
        """Authorize if we can, probe symbols, then (re)establish subscriptions."""
        await self._authorize_if_possible()
        symbols = await self.resolve_symbols()
        self.subscriptions_sent = 0
        for symbol in symbols:
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
                self.subscriptions_sent += 1
                # Deriv rate-limits bursts; requests fired back to back can
                # be rejected wholesale.
                await asyncio.sleep(SUBSCRIBE_STAGGER_S)
        for symbol in list(self._tick_symbols):
            await ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
        for cid in list(self._contract_ids):
            await ws.send(json.dumps(
                {"proposal_open_contract": 1, "contract_id": cid, "subscribe": 1}
            ))

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

    @staticmethod
    def _identify_candles(msg: dict) -> tuple[str | None, str | None]:
        """Work out which (symbol, timeframe) a candles payload belongs to.

        `passthrough` alone is not dependable — if the API stops echoing it,
        every valid response becomes unattributable and is dropped, which
        looks exactly like a dead feed (connected socket, zero streams).
        The echoed request itself always carries the symbol and granularity,
        so prefer that and keep passthrough as a fallback.
        """
        echo = msg.get("echo_req") or {}
        symbol = echo.get("ticks_history")
        tf = TF_OF_GRANULARITY.get(int(echo["granularity"])) if echo.get("granularity") else None
        if symbol and tf:
            return symbol, tf
        pt = echo.get("passthrough") or {}
        return pt.get("symbol") or symbol, pt.get("tf") or tf

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
            # Every error is recorded, including ones carrying a req_id from
            # a fire-and-forget subscription — those used to be dropped
            # silently, which hid failing candle subscriptions completely
            # (socket connected, zero streams, no explanation anywhere).
            err = msg["error"]
            echo = msg.get("echo_req", {})
            context = echo.get("passthrough") or {
                k: echo.get(k) for k in ("ticks_history", "ticks", "granularity") if echo.get(k)
            }
            entry = {
                "code": err.get("code"),
                "message": err.get("message"),
                "request": context,
                "at": datetime.now(UTC).isoformat(),
            }
            self.recent_errors.append(entry)
            log.error("deriv API error %s: %s (request: %s)",
                      err.get("code"), err.get("message"), context)
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
            symbol, tf = self._identify_candles(msg)
            if not symbol or not tf:
                log.error(
                    "candles response could not be attributed to a stream; "
                    "echo_req=%s", msg.get("echo_req"),
                )
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
