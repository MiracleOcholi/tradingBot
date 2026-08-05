"""Resolve a usable Deriv WebSocket URL.

Deriv now runs two generations of API side by side:

* legacy — `wss://ws.derivws.com/websockets/v3?app_id=<numeric>`, tokens
  issued per account. Credentials from the current dashboard are refused
  here: the app id fails the handshake with HTTP 401 and the PAT fails
  `authorize` with InvalidToken.
* current — REST at `https://api.derivws.com/trading/v1/...` authenticated
  with `Deriv-App-ID` + a bearer PAT, and a WebSocket opened with a
  short-lived OTP minted for one account:

      GET  /trading/v1/options/accounts              → the account list
      POST /trading/v1/options/accounts/{id}/otp     → a one-time password
      wss://…/trading/v1/options/ws/{demo|real}?otp=…

This module performs the current-generation handshake when a PAT is
available and falls back to the legacy URL when it is not, so the data
socket always has somewhere to connect.

Response shapes are read defensively — the OTP and account fields are
located by key name wherever they appear — because the payloads are not
published in a machine-readable schema.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("maverick.deriv.session")

REST_BASE = "https://api.derivws.com/trading/v1"
WS_BASE = "wss://api.derivws.com/trading/v1/options/ws"
LEGACY_WS = "wss://ws.derivws.com/websockets/v3"


def describe_shape(payload: Any, depth: int = 0) -> Any:
    """Key names and value TYPES only — never values.

    Used to learn an undocumented response layout without ever writing the
    credential it carries into logs or status output.
    """
    if depth > 4:
        return "..."
    if isinstance(payload, dict):
        return {k: describe_shape(v, depth + 1) for k, v in payload.items()}
    if isinstance(payload, list):
        return [describe_shape(payload[0], depth + 1)] if payload else []
    return type(payload).__name__


def _find_key(payload: Any, *names: str) -> Any:
    """Depth-first search for the first matching key. The payloads nest
    under `data`/`meta` inconsistently and are not schema-documented."""
    if isinstance(payload, dict):
        for name in names:
            if name in payload and payload[name] not in (None, ""):
                return payload[name]
        for value in payload.values():
            found = _find_key(value, *names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_key(item, *names)
            if found is not None:
                return found
    return None


class DerivSession:
    """Mints WebSocket URLs for the current-generation API."""

    def __init__(self, app_id: str, token: str | None, demo: bool = True) -> None:
        self.app_id = app_id
        self.token = token
        self.demo = demo
        self.accounts: list[dict] = []
        self.account_id: str | None = None
        self.last_error: str | None = None
        self.otp_response_shape: Any = None

    @property
    def usable(self) -> bool:
        return bool(self.app_id and self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Deriv-App-ID": self.app_id,
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def list_accounts(self, client: httpx.AsyncClient) -> list[dict]:
        r = await client.get(f"{REST_BASE}/options/accounts", headers=self._headers())
        r.raise_for_status()
        data = r.json()
        rows = data.get("data") if isinstance(data, dict) else None
        if isinstance(rows, dict):
            rows = rows.get("data")
        self.accounts = rows if isinstance(rows, list) else []
        return self.accounts

    def pick_account(self) -> str | None:
        """Choose the account matching the requested mode.

        `account_type` is 'demo' or 'real'; ids also carry the distinction
        (DOT… demo, ROT… real), so both signals are used.
        """
        wanted = "demo" if self.demo else "real"
        for acc in self.accounts:
            if str(acc.get("account_type", "")).lower() == wanted:
                return acc.get("account_id")
        prefix = "D" if self.demo else "R"
        for acc in self.accounts:
            if str(acc.get("account_id", "")).startswith(prefix):
                return acc.get("account_id")
        return None

    async def mint_otp(self, client: httpx.AsyncClient, account_id: str) -> str | None:
        r = await client.post(
            f"{REST_BASE}/options/accounts/{account_id}/otp",
            headers=self._headers(), json={},
        )
        r.raise_for_status()
        body = r.json()
        otp = _find_key(
            body, "otp", "one_time_password", "otp_code", "ws_otp",
            "code", "token", "access_token", "value",
        )
        if not otp:
            # No published schema, so when the expected field is absent we
            # record the response SHAPE — key names and value types only,
            # never values, since one of them is the credential itself.
            self.otp_response_shape = describe_shape(body)
            log.error("OTP field not found; response shape: %s", self.otp_response_shape)
        return str(otp) if otp else None

    async def websocket_url(self) -> str | None:
        """Full URL for the current-generation socket, or None if it cannot
        be built (caller then falls back to the legacy endpoint)."""
        if not self.usable:
            self.last_error = "no PAT stored; cannot use the current API"
            return None
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                await self.list_accounts(client)
                if not self.accounts:
                    self.last_error = "account list was empty"
                    return None
                account_id = self.pick_account()
                if not account_id:
                    self.last_error = (
                        f"no {'demo' if self.demo else 'real'} account among "
                        f"{[a.get('account_id') for a in self.accounts]}"
                    )
                    return None
                self.account_id = account_id
                otp = await self.mint_otp(client, account_id)
                if not otp:
                    self.last_error = "OTP missing from the response"
                    return None
        except httpx.HTTPStatusError as e:
            self.last_error = f"{e.response.status_code} from {e.request.url.path}: {e.response.text[:200]}"
            return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None

        self.last_error = None
        segment = "demo" if self.demo else "real"
        return f"{WS_BASE}/{segment}?otp={otp}"

    def status(self) -> dict:
        return {
            "usable": self.usable,
            "account_id": self.account_id,
            "accounts": [
                {"id": a.get("account_id"), "type": a.get("account_type"),
                 "currency": a.get("currency"), "balance": a.get("balance")}
                for a in self.accounts
            ],
            "last_error": self.last_error,
            "otp_response_shape": self.otp_response_shape,
        }
