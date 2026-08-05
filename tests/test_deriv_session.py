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
    otp_payload = otp_payload if otp_payload is not None else {
        "data": {"url": "wss://api.derivws.com/trading/v1/options/ws/demo?otp=OTP-XYZ"},
        "meta": {"endpoint": "/x", "method": "POST", "timing": 5},
    }

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
async def test_uses_the_url_the_api_returns(patched):
    """The endpoint returns a complete socket URL, not a bare OTP."""
    patched()
    s = DerivSession("app-id", "pat", demo=True)
    url = await s.websocket_url()
    assert url == "wss://api.derivws.com/trading/v1/options/ws/demo?otp=OTP-XYZ"
    assert s.account_id == "DOT93085490"
    assert s.last_error is None


async def test_real_account_selected_for_live(patched):
    patched()
    s = DerivSession("app-id", "pat", demo=False)
    await s.websocket_url()
    assert s.account_id == "ROT91793017"


async def test_bare_otp_still_supported_as_fallback(patched):
    patched(otp_payload={"otp": "FLAT"})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() == f"{WS_BASE}/demo?otp=FLAT"


async def test_non_websocket_url_is_rejected(patched):
    patched(otp_payload={"data": {"url": "https://not-a-socket"}})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() is None


async def test_missing_url_is_reported_not_guessed(patched):
    patched(otp_payload={"data": {}})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() is None
    assert "no socket URL" in s.last_error


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


def test_describe_shape_reports_types_never_values():
    from app.services.deriv_session import describe_shape

    shape = describe_shape({"data": {"otp_code": "SECRET-VALUE", "ttl": 60},
                            "rows": [{"id": "x"}]})
    assert shape == {"data": {"otp_code": "str", "ttl": "int"},
                     "rows": [{"id": "str"}]}
    assert "SECRET-VALUE" not in str(shape)


async def test_unknown_otp_field_records_shape_not_value(patched):
    patched(otp_payload={"result": {"pass_code": "SECRET"}})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() is None
    assert s.otp_response_shape == {"result": {"pass_code": "str"}}
    assert "SECRET" not in str(s.status())


async def test_alternate_otp_field_names_are_found(patched):
    patched(otp_payload={"data": {"otp_code": "ALT"}})
    s = DerivSession("app-id", "pat", demo=True)
    url = await s.websocket_url()
    assert url.endswith("otp=ALT")


async def test_url_field_wins_over_otp_field(patched):
    patched(otp_payload={"data": {"url": "wss://x/y?otp=A", "otp": "B"}})
    s = DerivSession("app-id", "pat", demo=True)
    assert await s.websocket_url() == "wss://x/y?otp=A"
