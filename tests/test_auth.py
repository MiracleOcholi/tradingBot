"""Dashboard auth: default→updatable credentials, lockout, session tokens."""
import time

import pytest

from app.services import auth


class FakeDB:
    def __init__(self):
        self.secrets: dict[str, dict] = {}

    async def select(self, table, query="", limit=None):
        name = query.split("name=eq.", 1)[1] if "name=eq." in query else None
        row = self.secrets.get(name)
        return [row] if row else []

    async def upsert(self, table, row, on_conflict):
        self.secrets[row["name"]] = row
        return [row]


@pytest.fixture(autouse=True)
def reset_lockout():
    auth._fails.update(count=0, locked_until=0.0)


@pytest.fixture
def db():
    return FakeDB()


async def test_default_credentials_work_until_changed(db):
    ok, _ = await auth.verify_login(db, "admin", "default")
    assert ok
    assert await auth.is_default_password(db)


async def test_wrong_credentials_rejected(db):
    for user, pw in [("admin", "wrong"), ("root", "default"), ("", "")]:
        ok, msg = await auth.verify_login(db, user, pw)
        assert not ok and "Invalid" in msg


async def test_change_credentials_and_defaults_stop_working(db):
    await auth.set_credentials(db, "maverick", "topgun-2026")
    ok, _ = await auth.verify_login(db, "maverick", "topgun-2026")
    assert ok
    ok, _ = await auth.verify_login(db, "admin", "default")
    assert not ok
    assert not await auth.is_default_password(db)
    assert await auth.current_username(db) == "maverick"


async def test_password_stored_hashed_never_plaintext(db):
    await auth.set_credentials(db, "maverick", "topgun-2026")
    stored = db.secrets[auth.AUTH_SECRET_NAME]["value_encrypted"]
    assert "topgun-2026" not in stored


async def test_lockout_after_max_failures(db):
    for _ in range(auth.MAX_FAILS):
        await auth.verify_login(db, "admin", "nope")
    ok, msg = await auth.verify_login(db, "admin", "default")  # correct, but locked
    assert not ok and "locked" in msg.lower()


async def test_successful_login_resets_fail_counter(db):
    for _ in range(auth.MAX_FAILS - 1):
        await auth.verify_login(db, "admin", "nope")
    ok, _ = await auth.verify_login(db, "admin", "default")
    assert ok
    assert auth._fails["count"] == 0


def test_token_round_trip():
    token = auth.issue_token("admin")
    assert auth.verify_token(token)


def test_expired_token_rejected():
    token = auth.issue_token("admin", now=time.time() - auth.TOKEN_TTL_S - 10)
    assert not auth.verify_token(token)


def test_tampered_token_rejected():
    token = auth.issue_token("admin")
    payload, _, sig = token.rpartition(".")
    assert not auth.verify_token(payload + "." + "0" * len(sig))
    assert not auth.verify_token("garbage")
    assert not auth.verify_token("")
