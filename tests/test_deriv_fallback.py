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
