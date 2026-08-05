# MAVERICK — MSnR Trading Assistant

Human-in-the-loop trading assistant for Deriv synthetic indices, built on the
**MSnR (Malaysian Support & Resistance)** methodology. One FastAPI process
(Render free tier) serving a cockpit-HUD dashboard, a Telegram signal loop
(Accept / Reject / Edit), and a background watcher that runs the MSnR engine —
**fully deterministic, no LLM anywhere in the trade decision path**.

The strategy spec is `MSnR_Methodology__2_.pdf` §8 (machine spec) + §6 — the
PDF is the law. Project decisions live in `HANDOFF-V2.md`.

## Architecture

```
FastAPI (single process, 512MB budget)
├── GET  /            cockpit dashboard (dark gunmetal · amber HUD)
├── GET  /health      keep-alive target (Cloudflare Worker cron */5 — Render
│                     free spins down after ~15 min idle)
├── POST /telegram    Telegram webhook (X-Telegram-Bot-Api-Secret-Token)
├── /api/*            dashboard state + controls
└── asyncio watcher   reminders → (B) Deriv candles + SNR → (C) state machines
                      → (D) Option-A emulated pending → multiplier execution
State: Supabase (secret key, backend only). Watcher is stateless between
cycles — levels, state machines, virtual orders, reminders reload on boot.
```

## Safety rails

- **Kill switch defaults OFF** — no order leaves the bot while it's off.
- **DEMO by default**; switching to LIVE forces the kill switch OFF.
- Max open trades + daily loss cap enforced before any execution.
- Every signal/decision/order/outcome row is tagged `DEMO`/`LIVE`.
- Secrets only in env vars (Koyeb) / encrypted-at-rest (Fernet) in Supabase.

## Setup (one-time)

1. **Database** — open the Supabase SQL Editor and run `db/schema.sql`, then
   `db/seed.sql` (idempotent; seed uses `ON CONFLICT DO NOTHING`). The app
   only *verifies* at startup (`app/bootstrap.py`) — API keys can't run DDL.
2. **Deploy** — Render: New + → Blueprint → this repo (`render.yaml`), or a
   manual web service with the same build/start commands. Env vars: copy the
   names from `.env.example` into the Render dashboard.
3. **Telegram webhook** —
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<app>.onrender.com/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>`
4. **Keep-alive** — deploy `worker/keepalive-worker.js` to Cloudflare with the
   `*/5 * * * *` cron (see `worker/wrangler.toml`).

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload   # http://localhost:8000
pytest                          # engine + math tests
```

## Build phases

- **A (this)** — FastAPI shell, dashboard, schema, Telegram mock loop,
  reminder engine, 1:4 edit-recompute + risk→stake math (tested).
- **B** — Deriv WS candles (D1/H4/H1/M15 × 7 symbols), SNR engine, charts.
- **C** — Direction + Setup state machines on live demo data.
- **D** — Option-A execution on the demo account.
- **E** — Validation → key rotation → live, small stakes.
- **F** — Deterministic outcome analytics (entry improvement).
