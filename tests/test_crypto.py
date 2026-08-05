"""Secrets-at-rest: key normalisation, round-trip, and clear failure modes."""
import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.services import crypto


@pytest.fixture
def set_key(monkeypatch):
    def _set(value):
        get_settings.cache_clear()
        monkeypatch.setenv("ENCRYPTION_KEY", value)
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


def test_real_fernet_key_used_verbatim(set_key):
    key = Fernet.generate_key().decode()
    set_key(key)
    assert crypto.normalise_key(key) == key.encode()
    assert crypto.key_status() == {"ok": True, "mode": "fernet-key"}


def test_hex_key_from_openssl_is_derived_not_rejected(set_key):
    # `openssl rand -hex 32` output — not a valid Fernet key on its own.
    hex_key = "9f" * 32
    set_key(hex_key)
    status = crypto.key_status()
    assert status["ok"] and status["mode"] == "derived-from-passphrase"
    assert crypto.decrypt(crypto.encrypt("deriv-token")) == "deriv-token"


def test_derivation_is_deterministic(set_key):
    set_key("a-long-enough-passphrase-for-maverick")
    token = crypto.encrypt("secret-value")
    get_settings.cache_clear()          # simulate a restart
    assert crypto.decrypt(token) == "secret-value"


def test_round_trip_with_proper_key(set_key):
    set_key(Fernet.generate_key().decode())
    assert crypto.decrypt(crypto.encrypt("hello")) == "hello"


def test_missing_key_raises_actionable_error(set_key):
    set_key("")
    with pytest.raises(crypto.SecretsUnavailable, match="not set"):
        crypto.encrypt("x")
    assert crypto.key_status()["ok"] is False


def test_short_key_rejected(set_key):
    set_key("tooshort")
    with pytest.raises(crypto.SecretsUnavailable, match="too short"):
        crypto.encrypt("x")
    assert crypto.key_status()["ok"] is False


def test_decrypt_after_key_change_explains_itself(set_key):
    set_key(Fernet.generate_key().decode())
    token = crypto.encrypt("value")
    set_key(Fernet.generate_key().decode())      # operator rotated the key
    with pytest.raises(crypto.SecretsUnavailable, match="ENCRYPTION_KEY changed"):
        crypto.decrypt(token)
