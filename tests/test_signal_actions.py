"""Web/Telegram shared signal lifecycle: apply_edit + accept/reject handling."""
import pytest

import app.services.signals as sigsvc


class FakeDB:
    def __init__(self, signals):
        self.signals = {s["id"]: s for s in signals}
        self.decisions = []

    async def get_signal(self, sid):
        return self.signals.get(sid)

    async def update_signal(self, sid, patch):
        self.signals[sid].update(patch)
        return self.signals[sid]

    async def record_decision(self, sid, action, payload=None):
        self.decisions.append({"signal_id": sid, "action": action, "payload": payload or {}})

    async def latest_awaiting_edit(self):
        for s in self.signals.values():
            if s.get("awaiting_edit_field") and s["status"] == "PENDING":
                return s
        return None

    async def get_config(self):
        return {"account_mode": "DEMO", "kill_switch": False}


class FakeTG:
    def __init__(self):
        self.updates = []

    async def update_signal_card(self, sig, note="", keyboard=None):
        self.updates.append(note)

    def signal_keyboard(self, sid):
        return {}

    def edit_keyboard(self, sid):
        return {}

    async def send_text(self, text):
        self.updates.append(text)


BUY_SIG = {
    "id": "s1", "symbol": "R_10", "side": "BUY", "status": "PENDING",
    "entry": 100.0, "sl": 98.0, "tp": 108.0, "is_mock": True,
    "account_mode": "DEMO", "awaiting_edit_field": None,
    "telegram_message_id": 1,
}


@pytest.fixture
def env(monkeypatch):
    db = FakeDB([dict(BUY_SIG)])
    tg = FakeTG()
    monkeypatch.setattr(sigsvc, "get_db", lambda: db)
    monkeypatch.setattr(sigsvc, "get_telegram", lambda: tg)
    return db, tg


async def test_apply_edit_entry_recomputes_tp_keeps_sl(env):
    db, tg = env
    sig, _msg = await sigsvc.apply_edit("s1", "entry", 101.0)
    assert sig is not None
    assert sig["sl"] == 98.0                      # structural stop untouched
    assert sig["tp"] == pytest.approx(101.0 + 4 * 3.0)
    assert db.decisions[0]["action"] == "EDIT"
    assert db.decisions[0]["payload"]["field"] == "entry"
    assert tg.updates                             # telegram card refreshed


async def test_apply_edit_tp_recomputes_sl(env):
    _db, _ = env
    sig, _ = await sigsvc.apply_edit("s1", "tp", 104.0)   # reward 4 → risk 1
    assert sig["sl"] == pytest.approx(99.0)
    assert sig["entry"] == 100.0


async def test_apply_edit_rejects_bad_values(env):
    sig, msg = await sigsvc.apply_edit("s1", "entry", 90.0)   # below its stop
    assert sig is None and "Invalid entry" in msg
    sig, msg = await sigsvc.apply_edit("s1", "stake", 50.0)
    assert sig is None and "Unknown field" in msg
    sig, msg = await sigsvc.apply_edit("missing", "entry", 100.0)
    assert sig is None and "not found" in msg


async def test_apply_edit_rejects_non_pending(env):
    db, _ = env
    db.signals["s1"]["status"] = "ACCEPTED"
    sig, msg = await sigsvc.apply_edit("s1", "entry", 101.0)
    assert sig is None and "no longer editable" in msg


async def test_reject_flow(env):
    db, _tg = env
    result = await sigsvc.handle_action("s1", "reject")
    assert result == "Rejected"
    assert db.signals["s1"]["status"] == "REJECTED"
    assert db.decisions[0]["action"] == "REJECT"


async def test_accept_mock_does_not_arm(env):
    db, _ = env
    result = await sigsvc.handle_action("s1", "accept")
    assert result == "Accepted"
    assert db.signals["s1"]["status"] == "ACCEPTED"


async def test_double_action_is_idempotent_noop(env):
    db, _ = env
    await sigsvc.handle_action("s1", "reject")
    result = await sigsvc.handle_action("s1", "accept")
    assert "Already" in result
    assert db.signals["s1"]["status"] == "REJECTED"


async def test_telegram_reply_path_uses_shared_edit(env):
    db, _ = env
    db.signals["s1"]["awaiting_edit_field"] = "sl"
    err = await sigsvc.apply_edit_value("99")
    assert err is None
    assert db.signals["s1"]["sl"] == 99.0
    assert db.signals["s1"]["tp"] == pytest.approx(104.0)
