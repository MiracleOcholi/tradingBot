"""Setup state machine — Phase C implementation target.

States (PDF §8.3): IDLE → DIRECTION_SET → AWAIT_TAP → REJECTION_PENDING →
M15_BREAK_PENDING → H1_ENGULF_PENDING → SETUP_CONFIRMED.

Invalidations (PDF §6.8) — any of these before the confirming H1 engulfing
closes returns the machine to IDLE:
 1. H4 candle body-closes beyond the Daily SNR.
 2. Daily candle body-closes beyond the Daily SNR.
 3. H1 produces its own structural (swing) break — only M15 may.
 4. M15 structural break fails to occur before an H4 close beyond (rule 4B).
 5. Direction flips first (opposite Daily rejection + opposite H4 break).

Definitions:
- Swing = extreme + ≥1 opposing candle (PDF §6.4).
- M15 break = wick-to-body: M15 body closes beyond the wick extreme of the
  last M15 swing.
- H1 engulfing = body close beyond previous candle's FULL wick; Type 1
  (single candle) or Type 2 (2+ consecutive same-colour candles, final one
  closes beyond); mixed colours invalid. H1 must NOT break structure.
- Entry = M15 order block (the engulfed candle, wick-to-wick zone), entry at
  the proximal edge; SL beyond the post-break strong high/low; TP fixed 1:4.
- A played Daily SNR loses freshness permanently for this setup.

State (per symbol) persists in engine_state.setup_state / state_payload so
the watcher survives Koyeb sleeps and redeploys.
"""
