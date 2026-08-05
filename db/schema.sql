-- Maverick — schema v2 (one-time apply via Supabase SQL Editor or Supabase MCP).
-- API keys cannot run DDL; app/bootstrap.py only VERIFIES these tables exist.
-- Idempotent: safe to re-run.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- config
-- Single-row runtime configuration, editable from the dashboard.
create table if not exists public.config (
  id              int primary key default 1 check (id = 1),
  kill_switch     boolean     not null default false,  -- OFF = no orders leave the bot
  account_mode    text        not null default 'DEMO' check (account_mode in ('DEMO','LIVE')),
  risk_per_trade  numeric     not null default 0.01,   -- fraction of balance risked at SL
  max_open_trades int         not null default 1,
  daily_loss_cap  numeric     not null default 0.05,   -- fraction of balance; trading halts for the day
  watchlist       jsonb       not null default '["R_10","R_50","R_75","1HZ150V","JD10","JD75","JD100"]'::jsonb,
  mock_signals    boolean     not null default true,   -- Phase A: telegram mock loop enabled
  updated_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------- secrets
-- Encrypted-at-rest values (Deriv demo/live tokens). Fernet via ENCRYPTION_KEY.
create table if not exists public.secrets (
  name            text primary key,          -- e.g. 'deriv_token_demo', 'deriv_token_live'
  value_encrypted text        not null,
  updated_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------- snr_levels
-- Every SNR is a single price point marked at the FIRST candle's close (PDF §4).
create table if not exists public.snr_levels (
  id            uuid primary key default gen_random_uuid(),
  symbol        text        not null,
  timeframe     text        not null check (timeframe in ('D1','H4','H1','M15')),
  price         numeric     not null,               -- close of 1st candle; never moves
  formation     text        not null check (formation in ('TRAD_R','TRAD_S','OC_R','OC_S')),
  role          text        not null check (role in ('S','R')),  -- current role (flips via SBR/RBS)
  break_count   int         not null default 0,     -- 0=original, 1=SBR/RBS, 2=Left Shoulder
  fresh         boolean     not null default true,  -- zero prior touches at first arrival
  touches       int         not null default 0,
  played        boolean     not null default false, -- a retracement played from it → never fresh again
  first_candle_at timestamptz not null,             -- open time of the 1st forming candle
  active        boolean     not null default true,
  created_at    timestamptz not null default now(),
  unique (symbol, timeframe, first_candle_at, formation)
);
create index if not exists snr_levels_lookup on public.snr_levels (symbol, timeframe, active);

-- ---------------------------------------------------------------- engine_state
-- Watcher is stateless between cycles: Direction + Setup state machine per symbol
-- persist here and are reloaded on boot (survives Koyeb sleeps/redeploys).
create table if not exists public.engine_state (
  symbol           text primary key,
  direction        text check (direction in ('BULLISH','BEARISH')),  -- null = none confirmed
  direction_since  timestamptz,
  setup_state      text        not null default 'IDLE' check (setup_state in
                     ('IDLE','DIRECTION_SET','AWAIT_TAP','REJECTION_PENDING',
                      'M15_BREAK_PENDING','H1_ENGULF_PENDING','SETUP_CONFIRMED')),
  state_payload    jsonb       not null default '{}'::jsonb,  -- tapped level id, m15 swing, order block, etc.
  updated_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------- signals
create table if not exists public.signals (
  id                  uuid primary key default gen_random_uuid(),
  symbol              text        not null,
  side                text        not null check (side in ('BUY','SELL')),
  account_mode        text        not null check (account_mode in ('DEMO','LIVE')),
  entry               numeric     not null,   -- proximal edge of the M15 order block
  sl                  numeric     not null,   -- beyond post-break strong high/low
  tp                  numeric     not null,   -- fixed 1:4 from entry & sl
  status              text        not null default 'PENDING' check (status in
                        ('PENDING','ACCEPTED','REJECTED','EXPIRED','FILLED','CLOSED','INVALIDATED')),
  is_mock             boolean     not null default false,
  order_block         jsonb,                  -- {high, low, open, close, ts} of engulfed M15 candle
  context             jsonb       not null default '{}'::jsonb, -- daily SNR, direction, H1 engulf info
  telegram_message_id bigint,
  awaiting_edit_field text check (awaiting_edit_field in ('entry','sl','tp')),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index if not exists signals_status on public.signals (status, created_at desc);

-- ---------------------------------------------------------------- decisions
-- Every human action on a signal (accept / reject / edit) — the audit trail.
create table if not exists public.decisions (
  id         uuid primary key default gen_random_uuid(),
  signal_id  uuid        not null references public.signals(id) on delete cascade,
  action     text        not null check (action in ('ACCEPT','REJECT','EDIT')),
  payload    jsonb       not null default '{}'::jsonb,  -- for EDIT: {field, old, new, recomputed:{...}}
  decided_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- virtual_orders
-- Option A: bot-emulated pending orders. Armed on Accept; fires a market
-- multiplier when a tick touches the entry level.
create table if not exists public.virtual_orders (
  id           uuid primary key default gen_random_uuid(),
  signal_id    uuid        not null references public.signals(id) on delete cascade,
  symbol       text        not null,
  side         text        not null check (side in ('BUY','SELL')),
  account_mode text        not null check (account_mode in ('DEMO','LIVE')),
  entry_price  numeric     not null,
  sl_price     numeric     not null,
  tp_price     numeric     not null,
  status       text        not null default 'ARMED' check (status in
                 ('ARMED','TRIGGERED','CANCELLED','EXPIRED')),
  armed_at     timestamptz not null default now(),
  triggered_at timestamptz
);
create index if not exists virtual_orders_armed on public.virtual_orders (status, symbol);

-- ---------------------------------------------------------------- trades
create table if not exists public.trades (
  id           uuid primary key default gen_random_uuid(),
  signal_id    uuid references public.signals(id) on delete set null,
  account_mode text        not null check (account_mode in ('DEMO','LIVE')),
  symbol       text        not null,
  side         text        not null check (side in ('BUY','SELL')),
  contract_id  text,
  stake        numeric,
  multiplier   int,
  entry_price  numeric,
  sl_price     numeric,
  tp_price     numeric,
  status       text        not null default 'OPEN' check (status in ('OPEN','WON','LOST','CLOSED_MANUAL','ERROR')),
  pnl          numeric,
  raw          jsonb       not null default '{}'::jsonb,
  opened_at    timestamptz not null default now(),
  closed_at    timestamptz
);
create index if not exists trades_open on public.trades (status, account_mode);

-- ---------------------------------------------------------------- reminders
-- Key-rotation reminder engine: every 3.5 months; resends +1d/+2d/+4d (max 3).
create table if not exists public.reminders (
  id           uuid primary key default gen_random_uuid(),
  name         text        not null unique,
  next_due     date        not null,
  resend_count int         not null default 0,       -- 0..3
  last_sent_at timestamptz,
  overdue      boolean     not null default false,   -- amber chip on dashboard after 3 resends
  history      jsonb       not null default '[]'::jsonb,  -- [{done_at, was_due}]
  created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------- RLS
-- Backend uses the SECRET key (bypasses RLS). The publishable key gets no
-- policies here → anon/browser access is denied; the dashboard talks to the
-- FastAPI /api layer instead.
alter table public.config         enable row level security;
alter table public.secrets        enable row level security;
alter table public.snr_levels     enable row level security;
alter table public.engine_state   enable row level security;
alter table public.signals        enable row level security;
alter table public.decisions      enable row level security;
alter table public.virtual_orders enable row level security;
alter table public.trades         enable row level security;
alter table public.reminders      enable row level security;
