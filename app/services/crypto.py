"""Fernet encryption for secrets-at-rest (Deriv tokens in the secrets table).

ENCRYPTION_KEY may be either a real Fernet key (32 url-safe base64 bytes) or
any sufficiently long secret — anything else is deterministically stretched
to a valid key with SHA-256. That keeps a perfectly good
`openssl rand -hex 32` value from hard-failing the whole secrets flow.

Derivation is deterministic, so ciphertext stays readable across restarts as
long as ENCRYPTION_KEY itself does not change. Changing the value makes
previously stored secrets undecryptable — re-enter them from the dashboard.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

MIN_KEY_LEN = 16


class SecretsUnavailable(Exception):
    """ENCRYPTION_KEY missing or too weak to encrypt with."""


def normalise_key(key: str) -> bytes:
    """Return a valid Fernet key: verbatim if it already is one, else derived."""
    candidate = key.strip().encode()
    try:
        Fernet(candidate)
        return candidate
    except (ValueError, TypeError):
        return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())


def _fernet() -> Fernet:
    key = (get_settings().encryption_key or "").strip()
    if not key:
        raise SecretsUnavailable(
            "ENCRYPTION_KEY is not set — add it to the Render environment, "
            "then redeploy before storing tokens."
        )
    if len(key) < MIN_KEY_LEN:
        raise SecretsUnavailable(
            f"ENCRYPTION_KEY is too short ({len(key)} chars, need ≥{MIN_KEY_LEN})."
        )
    return Fernet(normalise_key(key))


def key_status() -> dict:
    """Health-check view: is at-rest encryption usable, and how was the key read?"""
    key = (get_settings().encryption_key or "").strip()
    if not key:
        return {"ok": False, "reason": "ENCRYPTION_KEY not set"}
    if len(key) < MIN_KEY_LEN:
        return {"ok": False, "reason": f"ENCRYPTION_KEY too short ({len(key)} chars)"}
    verbatim = normalise_key(key) == key.encode()
    return {"ok": True, "mode": "fernet-key" if verbatim else "derived-from-passphrase"}


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise SecretsUnavailable(
            "Stored secret could not be decrypted — ENCRYPTION_KEY changed "
            "since it was saved. Re-enter the token from the dashboard."
        ) from e
