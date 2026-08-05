"""Dashboard/API endpoints. Mutations optionally guarded by DASHBOARD_PASSWORD."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.config import WATCHLIST, get_settings
from app.services import auth as auth_svc
from app.services import crypto, watcher
from app.services import signals as signal_svc
from app.services.deriv import GRANULARITY
from app.services.market import get_market
from app.services.supabase import get_db

log = logging.getLogger("maverick.api")
router = APIRouter(prefix="/api")


def require_auth(authorization: str = Header(default="")) -> None:
    """ALL /api endpoints require a valid session token from /api/login
    (default admin/default until changed). DASHBOARD_PASSWORD, if set, is
    also accepted as a static bearer key for scripts/curl."""
    token = authorization.removeprefix("Bearer ").strip()
    pw = get_settings().dashboard_password
    if pw and token and token == pw:
        return
    if auth_svc.verify_token(token):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn) -> dict:
    ok, msg = await auth_svc.verify_login(get_db(), body.username.strip(), body.password)
    if not ok:
        raise HTTPException(401, msg)
    return {
        "token": auth_svc.issue_token(body.username.strip()),
        "default_password": await auth_svc.is_default_password(get_db()),
    }


class CredsIn(BaseModel):
    current_password: str
    new_username: str = ""
    new_password: str


@router.post("/auth/change", dependencies=[Depends(require_auth)])
async def change_credentials(body: CredsIn) -> dict:
    db = get_db()
    current_user = await auth_svc.current_username(db)
    ok, msg = await auth_svc.verify_login(db, current_user, body.current_password)
    if not ok:
        raise HTTPException(401, f"Current password check failed: {msg}")
    new_user = body.new_username.strip() or current_user
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    if body.new_password == auth_svc.DEFAULT_PASSWORD:
        raise HTTPException(400, "Pick something other than the default password")
    await auth_svc.set_credentials(db, new_user, body.new_password)
    return {"result": "credentials updated", "username": new_user}


@router.get("/state", dependencies=[Depends(require_auth)])
async def get_state() -> dict[str, Any]:
    db = get_db()
    cfg = await db.get_config()
    sigs = await db.select("signals", "order=created_at.desc", limit=30)
    rems = await db.select("reminders", "")
    open_trades = await db.select("trades", "status=eq.OPEN")
    s = get_settings()
    return {
        "config": cfg,
        "signals": sigs,
        "reminders": rems,
        "open_trades": open_trades,
        "watcher": watcher.status(),
        # Configured values live behind auth, never on public /health.
        "env": {
            "deriv_app_id": s.deriv_app_id_clean or None,
            "deriv_app_id_valid": s.deriv_app_id_valid,
            "telegram_configured": s.telegram_configured,
        },
    }


@router.get("/candles", dependencies=[Depends(require_auth)])
async def get_candles(symbol: str, tf: str = "M15", limit: int = 200) -> dict:
    if symbol not in WATCHLIST:
        raise HTTPException(400, f"unknown symbol: {symbol}")
    if tf not in GRANULARITY:
        raise HTTPException(400, f"tf must be one of {list(GRANULARITY)}")
    return get_market().chart_data(symbol, tf, min(limit, 400))


class ConfigPatch(BaseModel):
    risk_per_trade: float | None = None
    max_open_trades: int | None = None
    daily_loss_cap: float | None = None
    watchlist: list[str] | None = None
    mock_signals: bool | None = None


@router.post("/config", dependencies=[Depends(require_auth)])
async def patch_config(patch: ConfigPatch) -> dict:
    data = {k: v for k, v in patch.model_dump().items() if v is not None}
    if "risk_per_trade" in data and not 0 < data["risk_per_trade"] <= 0.05:
        raise HTTPException(400, "risk_per_trade must be in (0, 0.05]")
    if "watchlist" in data:
        bad = [s for s in data["watchlist"] if s not in WATCHLIST]
        if bad:
            raise HTTPException(400, f"unknown symbols: {bad}")
    return await get_db().update_config(data)


class KillSwitch(BaseModel):
    on: bool


@router.post("/killswitch", dependencies=[Depends(require_auth)])
async def set_killswitch(body: KillSwitch) -> dict:
    return await get_db().update_config({"kill_switch": body.on})


class AccountMode(BaseModel):
    mode: str  # DEMO | LIVE


@router.post("/account-mode", dependencies=[Depends(require_auth)])
async def set_account_mode(body: AccountMode) -> dict:
    mode = body.mode.upper()
    if mode not in ("DEMO", "LIVE"):
        raise HTTPException(400, "mode must be DEMO or LIVE")
    patch: dict = {"account_mode": mode}
    if mode == "LIVE":
        # Safety interlock: switching to LIVE always forces the kill switch OFF.
        patch["kill_switch"] = False
    return await get_db().update_config(patch)


class SignalAction(BaseModel):
    action: str  # accept | reject


@router.post("/signals/{signal_id}/action", dependencies=[Depends(require_auth)])
async def signal_action(signal_id: str, body: SignalAction) -> dict:
    """Web-app Accept/Reject — same lifecycle as the Telegram buttons (arms
    execution on accept, updates the Telegram card, records the decision)."""
    action = body.action.lower()
    if action not in ("accept", "reject"):
        raise HTTPException(400, "action must be accept or reject")
    result = await signal_svc.handle_action(signal_id, action)
    sig = await get_db().select("signals", f"id=eq.{signal_id}")
    return {"result": result, "signal": sig[0] if sig else None}


class SignalEdit(BaseModel):
    field: str   # entry | sl | tp
    value: float


@router.post("/signals/{signal_id}/edit", dependencies=[Depends(require_auth)])
async def signal_edit(signal_id: str, body: SignalEdit) -> dict:
    """Web-app Edit: change one value; server recomputes the rest (1:4 held)."""
    sig, msg = await signal_svc.apply_edit(signal_id, body.field.lower(), body.value)
    if sig is None:
        raise HTTPException(400, msg)
    return {"result": msg, "signal": sig}


@router.get("/telegram/status", dependencies=[Depends(require_auth)])
async def telegram_status(request: Request) -> dict:
    """Webhook health. If inline buttons spin forever, the answer is here:
    usually `url` is empty (setWebhook never ran) or last_error_message
    shows Telegram being rejected (403 = secret mismatch)."""
    from app.api.telegram_webhook import stats as webhook_stats
    from app.services.telegram import get_telegram

    s = get_settings()
    info = await get_telegram().webhook_info() if s.telegram_configured else None
    result = (info or {}).get("result", {})
    expected = str(request.base_url).rstrip("/") + "/telegram"
    return {
        "configured": s.telegram_configured,
        "secret_set": bool(s.telegram_webhook_secret),
        "expected_url": expected,
        # What actually reached this process — if a tap does not move
        # `last_update_at`, Telegram never delivered it here.
        "inbound": webhook_stats(),
        "webhook": {
            "url": result.get("url", ""),
            "pending_update_count": result.get("pending_update_count"),
            "last_error_message": result.get("last_error_message"),
            "last_error_date": result.get("last_error_date"),
            "has_custom_certificate": result.get("has_custom_certificate"),
        },
        "healthy": bool(result.get("url")) and result.get("url") == expected
        and not result.get("last_error_message"),
    }


@router.post("/telegram/webhook", dependencies=[Depends(require_auth)])
async def repair_webhook(request: Request) -> dict:
    """Point Telegram at this deployment using the configured secret."""
    from app.services.telegram import get_telegram

    s = get_settings()
    if not s.telegram_configured:
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    if not s.telegram_webhook_secret:
        raise HTTPException(400, "TELEGRAM_WEBHOOK_SECRET not set — the webhook would be rejected")
    base = str(request.base_url).rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(400, f"Telegram requires an https URL (got {base})")
    resp = await get_telegram().set_webhook(base, s.telegram_webhook_secret)
    if not resp or not resp.get("ok"):
        raise HTTPException(502, f"Telegram rejected setWebhook: {resp}")
    return {"result": "webhook set", "url": base + "/telegram"}


@router.get("/analytics", dependencies=[Depends(require_auth)])
async def analytics() -> dict:
    """Deterministic outcome stats + entry-improvement suggestions (Phase F)."""
    from app.services import analytics as analytics_svc
    return await analytics_svc.report()


class SecretIn(BaseModel):
    name: str   # deriv_token_demo | deriv_token_live
    value: str


@router.post("/secrets", dependencies=[Depends(require_auth)])
async def store_secret(body: SecretIn) -> dict:
    """Store a Deriv account token encrypted-at-rest (Fernet, ENCRYPTION_KEY).
    The plaintext is never persisted or echoed back."""
    if body.name not in ("deriv_token_demo", "deriv_token_live"):
        raise HTTPException(400, "unknown secret name")
    if not body.value.strip():
        raise HTTPException(400, "empty value")
    try:
        encrypted = crypto.encrypt(body.value.strip())
    except crypto.SecretsUnavailable as e:
        raise HTTPException(400, str(e)) from e
    try:
        await get_db().upsert(
            "secrets",
            {"name": body.name, "value_encrypted": encrypted, "updated_at": "now()"},
            on_conflict="name",
        )
    except Exception as e:
        log.exception("storing %s failed", body.name)
        raise HTTPException(502, f"Could not save to the database: {e}") from e
    return {"stored": body.name}


@router.get("/secrets", dependencies=[Depends(require_auth)])
async def list_secrets() -> dict:
    """Which Deriv tokens are stored (names + timestamps only, never values)."""
    rows = await get_db().select("secrets", "order=name.asc")
    return {
        "stored": [
            {"name": r["name"], "updated_at": r.get("updated_at")}
            for r in rows
            if r["name"].startswith("deriv_token_")
        ],
        "encryption": crypto.key_status(),
    }


@router.post("/mock-signal", dependencies=[Depends(require_auth)])
async def mock_signal(symbol: str | None = None) -> dict:
    cfg = await get_db().get_config()
    if not cfg.get("mock_signals"):
        raise HTTPException(400, "mock_signals disabled in config")
    return await signal_svc.create_mock_signal(symbol)
