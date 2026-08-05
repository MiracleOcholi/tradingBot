"""Phase F — deterministic outcome analytics & entry-improvement suggestions.

Everything here is fixed arithmetic over logged rows: counts, win rates,
R-multiples, and threshold rules. NO LLM, no randomness, no fitting — the
suggestions cite the numbers they were derived from and always stay inside
the strategy rules (the entry may sit anywhere inside the M15 order block;
SL/TP relationships are fixed by the spec and are never suggested against).

Two parts:
- ExcursionTracker: watches M15 closes for up to TRACK_H hours after each
  real signal and records into signals.context.excursion how far price
  penetrated the order block (0 = proximal edge, 1 = distal edge) and
  whether the TP level traded. This is the raw material for the
  fill-rate/entry-depth suggestion.
- summarize()/build_suggestions(): pure functions the API exposes.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from statistics import median

from app.core.models import Candle
from app.services.supabase import SupabaseClient, get_db

log = logging.getLogger("maverick.analytics")

TRACK_H = 48                 # excursion-tracking window per signal
MIN_SAMPLE_TRADES = 10       # trades needed before win-rate comparisons speak
MIN_MISSED_FOR_ENTRY_HINT = 5
ENTRY_HINT_SAFETY = 0.8      # suggest 80% of the observed median penetration


# ---------------------------------------------------------------------------
# Excursion tracking
# ---------------------------------------------------------------------------
class ExcursionTracker:
    def __init__(self, db: SupabaseClient | None = None) -> None:
        self.db = db or get_db()
        self.active: dict[str, dict] = {}   # signal_id -> working record

    async def load(self) -> None:
        """Resume tracking for recent real signals after a restart."""
        cutoff = (datetime.now(UTC) - timedelta(hours=TRACK_H)).isoformat()
        rows = await self.db.select(
            "signals",
            f"is_mock=is.false&created_at=gte.{cutoff}"
            "&status=in.(PENDING,ACCEPTED,FILLED)",
        )
        for sig in rows:
            self.register(sig)
        log.info("excursion tracker resumed for %d signals", len(self.active))

    def register(self, sig: dict) -> None:
        ob = sig.get("order_block") or {}
        if not ob.get("high") or not ob.get("low"):
            return
        self.active[sig["id"]] = {
            "symbol": sig["symbol"],
            "side": sig["side"],
            "entry": float(sig["entry"]),
            "tp": float(sig["tp"]),
            "ob_high": float(ob["high"]),
            "ob_low": float(ob["low"]),
            "since": datetime.fromisoformat(sig["created_at"].replace("Z", "+00:00")),
            "context": sig.get("context") or {},
            "max_price": None,
            "min_price": None,
        }

    async def on_m15_close(self, symbol: str, candle: Candle) -> None:
        now = datetime.now(UTC)
        for sig_id, rec in list(self.active.items()):
            if rec["symbol"] != symbol:
                continue
            if now - rec["since"] > timedelta(hours=TRACK_H):
                del self.active[sig_id]
                continue
            rec["max_price"] = max(candle.high, rec["max_price"] or candle.high)
            rec["min_price"] = min(candle.low, rec["min_price"] or candle.low)
            metrics = self._metrics(rec)
            rec["context"] = {**rec["context"], "excursion": metrics}
            try:
                await self.db.update(
                    "signals", f"id=eq.{sig_id}", {"context": rec["context"]}
                )
            except Exception:
                log.exception("excursion persist failed for %s", sig_id)

    @staticmethod
    def _metrics(rec: dict) -> dict:
        zone = rec["ob_high"] - rec["ob_low"]
        if rec["side"] == "BUY":
            # Retracement comes DOWN into the zone: depth from the top edge.
            depth = (rec["ob_high"] - rec["min_price"]) / zone if zone > 0 else 0.0
            tp_seen = rec["max_price"] >= rec["tp"]
            entry_touched = rec["min_price"] <= rec["entry"]
        else:
            depth = (rec["max_price"] - rec["ob_low"]) / zone if zone > 0 else 0.0
            tp_seen = rec["min_price"] <= rec["tp"]
            entry_touched = rec["max_price"] >= rec["entry"]
        return {
            "ob_penetration": round(max(depth, 0.0), 4),
            "tp_seen": tp_seen,
            "entry_touched": entry_touched,
            "max_price": rec["max_price"],
            "min_price": rec["min_price"],
        }


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------
def _bucket(stats: dict, key: str, trade: dict) -> None:
    b = stats.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "r_n": 0})
    pnl = float(trade.get("pnl") or 0)
    b["trades"] += 1
    b["wins"] += 1 if pnl > 0 else 0
    b["pnl"] = round(b["pnl"] + pnl, 2)
    risk = float((trade.get("raw") or {}).get("risk_amount") or 0)
    if risk > 0:
        b["r_sum"] = round(b["r_sum"] + pnl / risk, 3)
        b["r_n"] += 1


def _finalize(buckets: dict) -> dict:
    out = {}
    for key, b in buckets.items():
        out[key] = {
            "trades": b["trades"],
            "win_rate": round(b["wins"] / b["trades"], 3) if b["trades"] else None,
            "pnl": b["pnl"],
            "avg_r": round(b["r_sum"] / b["r_n"], 2) if b["r_n"] else None,
        }
    return out


def summarize(signals: list[dict], trades: list[dict], virtual_orders: list[dict]) -> dict:
    """Aggregate logged history into the dashboard stats block."""
    funnel: dict[str, int] = {}
    for s in signals:
        funnel[s["status"]] = funnel.get(s["status"], 0) + 1

    closed = [t for t in trades if t["status"] in ("WON", "LOST")]
    by_symbol: dict = {}
    by_side: dict = {}
    by_engulf: dict = {}
    sig_by_id = {s["id"]: s for s in signals}
    total = {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "r_n": 0}
    for t in closed:
        _bucket({"_": total}, "_", t)
        _bucket(by_symbol, t["symbol"], t)
        _bucket(by_side, t["side"], t)
        sig = sig_by_id.get(t.get("signal_id") or "")
        etype = (sig or {}).get("context", {}).get("engulf_type")
        if etype is not None:
            _bucket(by_engulf, f"type_{etype}", t)

    cancel_reasons: dict[str, int] = {}
    for vo in virtual_orders:
        if vo["status"] in ("CANCELLED", "EXPIRED"):
            reason = vo.get("reason") or "unknown"
            cancel_reasons[reason] = cancel_reasons.get(reason, 0) + 1

    slippage = []
    for t in closed:
        sig = sig_by_id.get(t.get("signal_id") or "")
        if sig and t.get("entry_price") is not None:
            slippage.append(abs(float(t["entry_price"]) - float(sig["entry"])))

    return {
        "funnel": funnel,
        "overall": _finalize({"_": total})["_"],
        "by_symbol": _finalize(by_symbol),
        "by_side": _finalize(by_side),
        "by_engulf_type": _finalize(by_engulf),
        "cancel_reasons": cancel_reasons,
        "avg_fill_slippage": round(sum(slippage) / len(slippage), 6) if slippage else None,
        "sample": {"signals": len(signals), "closed_trades": len(closed)},
    }


def build_suggestions(signals: list[dict], stats: dict) -> list[dict]:
    """Deterministic threshold rules → entry-improvement hints WITHIN the
    strategy rules. Each suggestion carries the numbers it came from."""
    out: list[dict] = []

    # 1) Missed fills: expired signals whose excursion shows a shallow dip
    #    into the zone that a deeper entry would have caught.
    missed = [
        s for s in signals
        if s["status"] == "EXPIRED" and not s.get("is_mock")
        and (s.get("context") or {}).get("excursion", {}).get("tp_seen")
        and not s["context"]["excursion"].get("entry_touched")
        and s["context"]["excursion"].get("ob_penetration", 0) > 0
    ]
    if len(missed) >= MIN_MISSED_FOR_ENTRY_HINT:
        depths = [s["context"]["excursion"]["ob_penetration"] for s in missed]
        med = median(depths)
        suggested = round(min(max(med * ENTRY_HINT_SAFETY, 0.0), 1.0), 2)
        out.append({
            "kind": "entry_depth",
            "message": (
                f"{len(missed)} setups reached TP without filling at the proximal "
                f"edge; median zone penetration before the run was {med:.0%}. "
                f"An entry at {suggested:.0%} of the order-block depth (still inside "
                f"the zone) would have caught most of them."
            ),
            "suggested_depth": suggested,
            "sample": len(missed),
        })

    # 2) Engulfing-type edge, only with a real sample on both sides.
    be = stats.get("by_engulf_type", {})
    t1, t2 = be.get("type_1"), be.get("type_2")
    if (t1 and t2 and t1["trades"] >= MIN_SAMPLE_TRADES
            and t2["trades"] >= MIN_SAMPLE_TRADES
            and abs((t1["win_rate"] or 0) - (t2["win_rate"] or 0)) >= 0.15):
            better, worse = (("Type 1", t1), ("Type 2", t2))
            if (t2["win_rate"] or 0) > (t1["win_rate"] or 0):
                better, worse = ("Type 2", t2), ("Type 1", t1)
            out.append({
                "kind": "engulf_type",
                "message": (
                    f"{better[0]} confirmations win {better[1]['win_rate']:.0%} "
                    f"({better[1]['trades']} trades) vs {worse[1]['win_rate']:.0%} "
                    f"for {worse[0]} ({worse[1]['trades']}). Informational — both "
                    f"remain valid per the spec."
                ),
            })

    # 3) Symbol drag: a symbol with a meaningful sample and negative avg R.
    for sym, b in stats.get("by_symbol", {}).items():
        if b["trades"] >= MIN_SAMPLE_TRADES and (b["avg_r"] or 0) <= -0.5:
            out.append({
                "kind": "symbol_drag",
                "message": (
                    f"{sym}: {b['trades']} trades, avg {b['avg_r']:+.2f}R, "
                    f"P/L {b['pnl']:+.2f}. Consider dropping it from the "
                    f"watchlist until demo results improve."
                ),
            })
    return out


async def report(db: SupabaseClient | None = None) -> dict:
    db = db or get_db()
    signals = await db.select("signals", "is_mock=is.false&order=created_at.desc", limit=500)
    trades = await db.select("trades", "order=opened_at.desc", limit=500)
    vorders = await db.select("virtual_orders", "order=armed_at.desc", limit=500)
    stats = summarize(signals, trades, vorders)
    return {"stats": stats, "suggestions": build_suggestions(signals, stats)}


_tracker: ExcursionTracker | None = None


def get_tracker() -> ExcursionTracker:
    global _tracker
    if _tracker is None:
        _tracker = ExcursionTracker()
    return _tracker
