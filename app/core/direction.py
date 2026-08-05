"""Direction confirmation — Phase C implementation target.

Contract (PDF §6.1):
- Bullish: Daily rejection of a fresh Daily Support + H4 body-to-body break
  of the last H4 TRADITIONAL Resistance before the tap.
- Bearish: mirror (fresh Daily Resistance + H4 body-to-body break of the last
  H4 Traditional Support).
- H4 body-to-body break = H4 body close beyond the SNR's body-close level.
- Direction stays valid until the MIRROR event (both opposite conditions).

Phase C fills in the evaluator over live candle streams, persisting to
engine_state.direction.
"""
