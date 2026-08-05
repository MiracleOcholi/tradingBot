"""Dashboard authentication.

- Credentials: single operator account, default admin/default until changed
  from the dashboard. Stored in the `secrets` table as PBKDF2-SHA256
  (200k iterations, per-credential salt) — never plaintext, independent of
  ENCRYPTION_KEY so login works before Fernet is configured.
- Sessions: HMAC-signed expiring tokens. The signing key derives from
  ENCRYPTION_KEY when set (sessions survive restarts); otherwise a per-boot
  random key (restart = re-login).
- Brute force: 5 failed logins → 60s lockout (in-memory; resets on restart,
  which is fine — it only needs to make guessing impractical).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
import time

from app.config import get_settings

log = logging.getLogger("maverick.auth")

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "default"
AUTH_SECRET_NAME = "dashboard_auth"
PBKDF2_ITERATIONS = 200_000
TOKEN_TTL_S = 7 * 24 * 3600
MAX_FAILS = 5
LOCKOUT_S = 60

_fails = {"count": 0, "locked_until": 0.0}
_boot_key = pysecrets.token_bytes(32)


def _signing_key() -> bytes:
    key = get_settings().encryption_key
    if key:
        return hashlib.sha256(f"maverick-auth:{key}".encode()).digest()
    return _boot_key


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt_hex = salt_hex or pysecrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return salt_hex, dk.hex()


class AuthBackendUnavailable(Exception):
    pass


async def _load_record(db) -> dict | None:
    try:
        rows = await db.select("secrets", f"name=eq.{AUTH_SECRET_NAME}")
    except Exception as e:
        raise AuthBackendUnavailable(str(e)) from e
    if not rows:
        return None
    try:
        return json.loads(rows[0]["value_encrypted"])
    except (ValueError, KeyError):
        log.error("corrupt dashboard_auth record — falling back to defaults")
        return None


async def set_credentials(db, username: str, password: str) -> None:
    salt, digest = hash_password(password)
    await db.upsert(
        "secrets",
        {
            "name": AUTH_SECRET_NAME,
            "value_encrypted": json.dumps(
                {"username": username, "salt": salt, "hash": digest}
            ),
        },
        on_conflict="name",
    )


async def verify_login(db, username: str, password: str) -> tuple[bool, str]:
    now = time.monotonic()
    if now < _fails["locked_until"]:
        return False, "Too many attempts — locked for a minute"

    try:
        record = await _load_record(db)
    except AuthBackendUnavailable:
        log.exception("auth store unreachable — failing login closed")
        return False, "Auth backend unavailable — try again shortly"
    if record is None:
        user_ok = hmac.compare_digest(username, DEFAULT_USERNAME)
        pass_ok = hmac.compare_digest(password, DEFAULT_PASSWORD)
    else:
        _, digest = hash_password(password, record["salt"])
        user_ok = hmac.compare_digest(username, record["username"])
        pass_ok = hmac.compare_digest(digest, record["hash"])

    if not (user_ok and pass_ok):
        _fails["count"] += 1
        if _fails["count"] >= MAX_FAILS:
            _fails["count"] = 0
            _fails["locked_until"] = now + LOCKOUT_S
            log.warning("dashboard login locked out for %ss", LOCKOUT_S)
        return False, "Invalid credentials"

    _fails["count"] = 0
    return True, "ok"


async def is_default_password(db) -> bool:
    try:
        return await _load_record(db) is None
    except AuthBackendUnavailable:
        return False


async def current_username(db) -> str:
    try:
        record = await _load_record(db)
    except AuthBackendUnavailable:
        return DEFAULT_USERNAME
    return record["username"] if record else DEFAULT_USERNAME


# ---------------------------------------------------------------- tokens
def issue_token(username: str, now: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": username, "exp": int((now or time.time()) + TOKEN_TTL_S)}).encode()
    ).decode()
    sig = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, TypeError):
        return False
    return (now or time.time()) < float(data.get("exp", 0))
