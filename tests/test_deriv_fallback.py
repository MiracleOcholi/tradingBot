"""Data socket must not go dark because the configured app id is refused.

Candle data is public. The current Deriv dashboard issues app ids the
legacy WebSocket endpoint rejects with HTTP 401, so the client falls back
to the public documentation app id for market data and reports that it did.
"""
from app.services.deriv import PUBLIC_FALLBACK_APP_ID, DerivClient


async def _noop(*a, **k):
    pass


def client(app_id="341ECn6ZnBXRon1hv5a4p", fallback=PUBLIC_FALLBACK_APP_ID):
    return DerivClient(app_id, ["R_10"], _noop, fallback_app_id=fallback)


def test_url_uses_configured_app_id_first():
    c = client()
    assert "app_id=341ECn6ZnBXRon1hv5a4p" in c.url
    assert c.status()["using_fallback_app_id"] is False


def test_switch_moves_url_to_public_app_id():
    c = client()
    assert c._switch_to_fallback() is True
    assert f"app_id={PUBLIC_FALLBACK_APP_ID}" in c.url
    s = c.status()
    assert s["active_app_id"] == PUBLIC_FALLBACK_APP_ID
    assert s["using_fallback_app_id"] is True


def test_switch_is_idempotent():
    c = client()
    assert c._switch_to_fallback() is True
    assert c._switch_to_fallback() is False      # already there; do not loop


def test_no_fallback_configured_means_no_switch():
    c = client(fallback="")
    assert c._switch_to_fallback() is False
    assert "app_id=341ECn6ZnBXRon1hv5a4p" in c.url


def test_already_using_public_id_does_not_switch():
    c = client(app_id=PUBLIC_FALLBACK_APP_ID)
    assert c._switch_to_fallback() is False
    assert c.status()["using_fallback_app_id"] is False


async def test_auth_state_records_success(monkeypatch):
    async def token():
        return "tok"

    c = DerivClient("1089", ["R_10"], _noop, token_provider=token)

    async def ok(t):
        return {"loginid": "CR123", "is_virtual": 0,
                "landing_company_name": "svg", "currency": "USD"}

    monkeypatch.setattr(c, "authorize", ok)
    await c._authorize_if_possible()
    assert c.status()["auth"] == {
        "attempted": True, "ok": True, "loginid": "CR123",
        "is_virtual": False, "landing_company": "svg", "currency": "USD",
    }


async def test_auth_state_records_failure_and_survives_error_burst(monkeypatch):
    async def token():
        return "bad"

    c = DerivClient("1089", ["R_10"], _noop, token_provider=token)

    async def boom(t):
        raise RuntimeError("InvalidToken")

    monkeypatch.setattr(c, "authorize", boom)
    await c._authorize_if_possible()
    for i in range(40):                       # flood the rotating buffer
        c.recent_errors.append({"code": "InvalidSymbol", "message": str(i)})
    auth = c.status()["auth"]
    assert auth["ok"] is False and "InvalidToken" in auth["error"]


async def test_auth_state_when_no_token_stored():
    c = DerivClient("1089", ["R_10"], _noop, token_provider=None)
    await c._authorize_if_possible()
    assert c.status()["auth"]["attempted"] is False
