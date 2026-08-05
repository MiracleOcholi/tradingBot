"""Current-generation Deriv handshake: accounts → OTP → WebSocket URL."""
import httpx
import pytest

from app.services.deriv_session import WS_BASE, DerivSession, _find_key

ACCOUNTS = {
    "data": [
        {"account_id": "ROT91793017", "account_type": "real",
         "currency": "USD", "balance": "0.00"},
        {"account_id": "DOT93085490", "account_type": "demo",
         "currency": "USD", "balance": "9913.81"},
    ]
}


def transport(accounts=ACCOUNTS, otp_payload=None, otp_status=200):
    otp_payload = otp_payload if otp_payload is not None else {"data": {"otp": "OTP-XYZ"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/otp"):
            return httpx.Response(otp_status, json=otp_payload)
        return httpx.Response(200, json=accounts)

    return httpx.MockTransport(handler)


@pytest.fixture
def patched(monkeypatch):
    def _apply(**kw):
        t = transport(**kw)
        real = httpx.AsyncClient

        def factory(*a, **k):
            return real(transport=t, **{x: y for x, y in k.items() if x != "transport"})

        monkeypatch.setattr(httpx, "AsyncClient", factory)
    return _apply


# ---------------------------------------------------------------- key search
def test_find_key_digs_through_nesting():
    assert _find_key({"data": {"inner": {"otp": "X"}}}, "otp") == "X"
    assert _find_key({"data": [{"otp": "Y"}]}, "otp") == "Y"
    assert _find_key({"a": 1}, "otp") is None
    assert _find_key({"otp": ""}, "otp") is None        # empty is not a value


# ---------------------------------------------------------------- account pick
def test_picks_demo_account_by_type():
    s = DerivSession("app", "tok", demo=True)
    s.accounts = ACCOUNTS["data"]
    assert s.pick_account() == "DOT93085490"


def test_picks_real_account_by_type():
    s = DerivSession("app", "tok", demo=False)
    s.accounts = ACCOUNTS["data"]
    assert s.pick_account() == "ROT91793017"


def test_falls_back_to_id_prefix_when_type_missing():
    s = DerivSession("app", "tok", demo=True)
    s.accounts = [{"account_id": "DOT1"}, {"account_id": "ROT1"}]
    assert s.pick_account() == "DOT1"


def test_no_matching_account_returns_none():
    s = DerivSession("app", "tok", demo=True)
    s.accounts = [{"account_id": "ROT1", "account_type": "real"}]
    assert s.pick_account() is None


# ---------------------------------------------------------------- full flow
async def test_builds_demo_websocket_url(patched):
    patched()
    s = DerivSession("app-id", "pat", demo=True)
    url = await s.websocket_url()
    assert url == f"{WS_BASE}/demo?otp=OTP-XYZ"
    assert s.account_id == "DOT93085490"
    assert s.last_error is None


async def test_builds_real_websocket_url(patched):
    patched()
    s = DerivSession("app-id", "pat", demo=False)
    assert await s.websocket_url() == f"{WS_BASE}/real?otp=OTP-XYZ"


async def test_otp_at_top_level_also_accepted(patched):
    patched(otp_payload={"otp": "FLAT"})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() == f"{WS_BASE}/demo?otp=FLAT"


async def test_missing_otp_is_reported_not_guessed(patched):
    patched(otp_payload={"data": {}})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() is None
    assert "OTP missing" in s.last_error


async def test_http_error_is_reported(patched):
    patched(otp_status=403, otp_payload={"errors": ["nope"]})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() is None
    assert "403" in s.last_error


async def test_empty_account_list_reported(patched):
    patched(accounts={"data": []})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() is None
    assert "empty" in s.last_error


async def test_without_a_token_the_session_is_unusable():
    s = DerivSession("app-id", None, demo=True)
    assert s.usable is False
    assert await s.websocket_url() is None
    assert "no PAT" in s.last_error
