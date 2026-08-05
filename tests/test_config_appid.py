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


def test_health_never_publishes_the_app_id(set_app_id):
    """/health is unauthenticated — it reports usability, never values."""
    from fastapi.testclient import TestClient

    import app.main as main

    for value in ("341ECn6ZnBXRon1hv5a4p", "1089"):
        set_app_id(value)
        with TestClient(main.app) as client:
            body = client.get("/health").text
        assert value not in body

    set_app_id("1089")
    with TestClient(main.app) as client:
        data = client.get("/health").json()
    assert data["deriv_app_id_set"] is True
    assert data["deriv_app_id_valid"] is True
