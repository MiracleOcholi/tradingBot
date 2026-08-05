# Draft: message to Deriv API support

Copy from the line below. Replace the bracketed bits before sending.
(If you rotated the app id after it was exposed, use the current one.)

---

**Subject:** New dashboard app ID rejected by WebSocket API (HTTP 401) — which API should a server-side bot use?

Hello,

I'm building a private, server-side trading assistant for my own account
(synthetic indices — Volatility and Jump). I've hit a wall connecting to the
API and would like guidance on which API surface I should be building
against, plus a few specifics.

**My setup**

- Application registered via the current dashboard as a **Native app (PAT)**
- App ID issued: `[YOUR_APP_ID]` (alphanumeric)
- Accounts on the token: `[REAL_ACCOUNT_ID]` (real) and `[DEMO_ACCOUNT_ID]` (demo)
- Backend: Python service, no browser, no OAuth redirect flow

**Problem 1 — the WebSocket API rejects my app ID**

Connecting to:

```
wss://ws.derivws.com/websockets/v3?app_id=[YOUR_APP_ID]
```

fails the handshake with **HTTP 401**, consistently. The same connection
with the public documentation app ID `1089` succeeds, so the endpoint and
my network path are fine — it is specifically my app ID that is refused.

My app ID *is* accepted by the newer REST API: a `GET` to
`https://api.derivws.com/trading/v1/options/accounts` with the header
`Deriv-App-ID: [YOUR_APP_ID]` returns my accounts correctly.

Questions:
1. Are app IDs issued by the current dashboard incompatible with
   `ws.derivws.com/websockets/v3`?
2. If so, can a new application still obtain a numeric app ID for that
   endpoint, or is it closed to new registrations?
3. Is `ws.derivws.com/websockets/v3` deprecated? If so, what is the
   supported replacement for server-side market data and trading, and is
   there a deprecation timeline?

**Problem 2 — `active_symbols` returns an empty list**

Using app ID `1089` (unauthenticated), the WebSocket connects, but:

```json
{"active_symbols": "brief"}
```

returns an **empty array**, and every `ticks_history` subscription then fails
with `InvalidSymbol` (e.g. "Symbol JD75 is invalid") — including symbols that
plainly exist, such as `R_10`.

Running the identical call from the API Playground while signed in returns
the full list (R_10, R_50, R_75, JD10, JD75, JD100, and so on).

Question:
4. Is an authorized session required for `active_symbols` to return
   instruments, or does the empty result indicate the app ID's landing
   company has no offerings? What is the correct way for a server-side app
   to retrieve market data?

**Problem 3 — no demo API token in the dashboard**

Previously, API tokens were created per account, so a token generated while
the virtual (VRTC) account was selected was demo-only. In the current
dashboard I can't find where to issue a demo-specific token — the PAT I
have appears to cover both my real and demo accounts.

Questions:
5. Is there still a way to issue a **demo-only** token?
6. If one token now covers multiple accounts, how does a server-side app
   select which account an order is placed on? Is there an account
   parameter, or must the connection be scoped some other way?

I care about this specifically because my software refuses to trade unless
the authorized account matches its configured DEMO/LIVE mode, and I want
demo validation to be genuinely incapable of touching the real account.

**Problem 4 — machine-readable reference**

7. Is there an OpenAPI/Swagger specification for the `/trading/v1` API?
   I could only find the interactive documentation, and I'd like to
   generate a client against the schema.

**What I need most:** a clear statement of which API a server-side bot
should target for (a) OHLC candle history and streaming on synthetic
indices, and (b) placing multiplier contracts with stop loss and take
profit — plus whether my dashboard app ID is the right credential for it.

Thanks very much,
[YOUR NAME]
