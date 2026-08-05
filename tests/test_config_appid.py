"""DERIV_APP_ID normalisation — quoted/whitespaced values cause the same
HTTP 401 as an unregistered id, so they must be cleaned before use."""
import pytest

from app.config import get_settings


@pytest.fixture
def set_app_id(monkeypatch):
    def _set(value):
        get_settings.cache_clear()
        monkeypatch.setenv("DERIV_APP_ID", value)
        get_settings.cache_clear()
        return get_settings()
    yield _set
    get_settings.cache_clear()


@pytest.mark.parametrize("raw", ["12345", " 12345 ", '"12345"', "'12345'", ' "12345" '])
def test_quotes_and_whitespace_stripped(set_app_id, raw):
    s = set_app_id(raw)
    assert s.deriv_app_id_clean == "12345"
    assert s.deriv_app_id_valid


def test_non_numeric_flagged(set_app_id):
    s = set_app_id("your_app_id_here")
    assert s.deriv_app_id_valid is False


def test_empty_is_not_valid(set_app_id):
    s = set_app_id("")
    assert s.deriv_app_id_clean == ""
    assert s.deriv_app_id_valid is False


def test_health_never_echoes_a_non_numeric_app_id(set_app_id, monkeypatch):
    """A non-numeric value is not an app id — it may be a token pasted into
    the wrong variable, so /health must not publish it."""
    from fastapi.testclient import TestClient

    secretish = "341ECn6ZnBXRon1hv5a4p"
    set_app_id(secretish)
    import app.main as main

    with TestClient(main.app) as client:
        body = client.get("/health").text
    assert secretish not in body
    assert "not a numeric app id" in body


def test_health_echoes_a_valid_app_id(set_app_id):
    from fastapi.testclient import TestClient

    set_app_id("1089")
    import app.main as main

    with TestClient(main.app) as client:
        data = client.get("/health").json()
    assert data["deriv_app_id"] == "1089"
    assert data["deriv_app_id_valid"] is True
