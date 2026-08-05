"""Option-A execution: trigger/expiry logic and fire-time guards.

Uses in-memory fakes for Supabase and Telegram; no network anywhere.
"""
from datetime import UTC, datetime, timedelta

import pytest

import app.execution.emulated_pending as ep
from app.execution.emulated_pending import ARM_TTL_H, ExecutionService


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {
            "virtual_orders": [], "trades": [], "signals": [], "secrets": [],
        }
        self.config = {
            "kill_switch": True, "account_mode": "DEMO", "risk_per_trade": 0.01,
            "max_open_trades": 1, "daily_loss_cap": 0.05,
        }
        self._id = 0

    def _match(self, row: dict, query: str) -> bool:
        for part in [p for p in query.split("&") if p and "=eq." in p]:
            field, value = part.split("=eq.", 1)
            if str(row.get(field)) != value:
                return False
        return True

    async def select(self, table, query="", limit=None):
        rows = [r for r in self.tables.get(table, []) if self._match(r, query)]
        return rows[:limit] if limit else rows

    async def insert(self, table, row):
        self._id += 1
        stored = {**row, "id": f"{table}-{self._id}",
                  "armed_at": datetime.now(UTC).isoformat()}
        self.tables[table].append(stored)
        return [stored]

    async def update(self, table, query, patch):
        out = []
        for r in self.tables.get(table, []):
            if self._match(r, query.split("&")[0]):
                r.update(patch)
                out.append(r)
        return out

    async def get_config(self):
        return dict(self.config)


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text):
        self.sent.append(text)


@pytest.fixture
def svc(monkeypatch):
    db = FakeDB()
    tg = FakeTelegram()
    monkeypatch.setattr(ep, "get_telegram", lambda: tg)
    service = ExecutionService(db=db)
    service.tg = tg  # test-side handle
    return service


SIGNAL = {
    "id": "sig-1", "symbol": "R_10", "side": "BUY", "account_mode": "DEMO",
    "entry": 100.0, "sl": 99.0, "tp": 104.0,
}


async def test_arm_creates_row_and_tracks(svc):
    vo = await svc.arm(SIGNAL)
    assert vo["id"] in svc.armed
    assert vo["entry_price"] == 100.0


async def test_buy_triggers_only_at_or_below_entry(svc):
    await svc.arm(SIGNAL)
    fired = []

    async def fake_fire(v, q):
        fired.append(q)

    svc._fire = fake_fire
    await svc.on_tick("R_10", 100.5, 0)      # above entry → nothing
    assert fired == []
    await svc.on_tick("R_10", 100.0, 1)      # touch
    assert fired == [100.0]


async def test_sell_triggers_at_or_above_entry(svc):
    sell = {**SIGNAL, "id": "sig-2", "side": "SELL", "entry": 100.0,
            "sl": 101.0, "tp": 96.0}
    await svc.arm(sell)
    fired = []

    async def fake_fire(v, q):
        fired.append(q)

    svc._fire = fake_fire
    await svc.on_tick("R_10", 99.8, 0)
    assert fired == []
    await svc.on_tick("R_10", 100.2, 1)
    assert fired == [100.2]


async def test_tp_traded_before_entry_expires_buy_order(svc):
    vo = await svc.arm(SIGNAL)               # BUY entry 100, tp 104
    await svc.on_tick("R_10", 104.5, 0)      # price ran up through TP unfilled
    assert vo["id"] not in svc.armed
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "EXPIRED"


async def test_tp_traded_before_entry_expires_sell_order(svc):
    sell = {**SIGNAL, "id": "sig-3", "side": "SELL", "entry": 100.0,
            "sl": 101.0, "tp": 96.0}
    vo = await svc.arm(sell)
    await svc.on_tick("R_10", 95.5, 0)       # price fell through TP unfilled
    assert vo["id"] not in svc.armed
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "EXPIRED"


async def test_ttl_expiry(svc):
    vo = await svc.arm(SIGNAL)
    svc.armed[vo["id"]]["armed_at"] = (
        datetime.now(UTC) - timedelta(hours=ARM_TTL_H + 1)
    ).isoformat()
    await svc.on_tick("R_10", 101.0, 0)
    assert vo["id"] not in svc.armed
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "EXPIRED"


async def test_kill_switch_off_cancels_at_touch(svc):
    svc.db.config["kill_switch"] = False
    vo = await svc.arm(SIGNAL)
    await svc.on_tick("R_10", 100.0, 0)      # touch → guard fails
    assert vo["id"] not in svc.armed
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "CANCELLED"
    assert any("kill switch" in t for t in svc.tg.sent)


async def test_account_mode_mismatch_cancels(svc):
    svc.db.config["account_mode"] = "LIVE"
    await svc.arm(SIGNAL)                    # DEMO order
    await svc.on_tick("R_10", 100.0, 0)
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "CANCELLED"
    assert any("DEMO" in t for t in svc.tg.sent)


async def test_max_open_trades_blocks(svc):
    svc.db.tables["trades"].append(
        {"id": "t1", "status": "OPEN", "account_mode": "DEMO"}
    )
    await svc.arm(SIGNAL)
    await svc.on_tick("R_10", 100.0, 0)
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "CANCELLED"
    assert any("max open trades" in t for t in svc.tg.sent)


async def test_missing_token_cancels(svc):
    await svc.arm(SIGNAL)                    # no secrets stored
    await svc.on_tick("R_10", 100.0, 0)
    row = svc.db.tables["virtual_orders"][0]
    assert row["status"] == "CANCELLED"
    assert any("token" in t for t in svc.tg.sent)


async def test_multiplier_pick_prefers_smallest_viable(svc):
    from app.core.entry import build_plan
    from app.core.models import Side

    svc._mult_cache["R_10"] = [30, 100, 200, 500]
    plan = build_plan(Side.BUY, 100.0, 99.0)     # 1% move
    # balance 1000, risk 1% → risk $10; stake(m) = 10/(m*0.01) = 1000/m
    # m=30 → $33 (>20% of 1000? no, 33 ≤ 200 ✓) → picks 30
    m = await svc._pick_multiplier("R_10", plan, 1000.0, 0.01)
    assert m == 30


async def test_multiplier_pick_rejects_substake(svc):
    from app.core.entry import build_plan
    from app.core.models import Side

    svc._mult_cache["R_10"] = [2000]
    plan = build_plan(Side.BUY, 100.0, 99.0)
    # stake = 10/(2000*0.01) = $0.50 < $1 minimum → None
    m = await svc._pick_multiplier("R_10", plan, 1000.0, 0.01)
    assert m is None
