"""Dashboard/API endpoints. Mutations optionally guarded by DASHBOARD_PASSWORD."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import WATCHLIST, get_settings
from app.services import signals as signal_svc
from app.services import watcher
from app.services.deriv import GRANULARITY
from app.services.market import get_market
from app.services.supabase import get_db

log = logging.getLogger("maverick.api")
router = APIRouter(prefix="/api")


def require_auth(authorization: str = Header(default="")) -> None:
    """Bearer check for mutating endpoints; open when no password is set (dev)."""
    pw = get_settings().dashboard_password
    if pw and authorization != f"Bearer {pw}":
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/state")
async def get_state() -> dict[str, Any]:
    db = get_db()
    cfg = await db.get_config()
    sigs = await db.select("signals", "order=created_at.desc", limit=30)
    rems = await db.select("reminders", "")
    open_trades = await db.select("trades", "status=eq.OPEN")
    return {
        "config": cfg,
        "signals": sigs,
        "reminders": rems,
        "open_trades": open_trades,
        "watcher": watcher.status(),
    }


@router.get("/candles")
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


@router.get("/analytics")
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
    from app.services import crypto

    if body.name not in ("deriv_token_demo", "deriv_token_live"):
        raise HTTPException(400, "unknown secret name")
    if not body.value.strip():
        raise HTTPException(400, "empty value")
    try:
        encrypted = crypto.encrypt(body.value.strip())
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    await get_db().upsert(
        "secrets",
        {"name": body.name, "value_encrypted": encrypted, "updated_at": "now()"},
        on_conflict="name",
    )
    return {"stored": body.name}


@router.post("/mock-signal", dependencies=[Depends(require_auth)])
async def mock_signal(symbol: str | None = None) -> dict:
    cfg = await get_db().get_config()
    if not cfg.get("mock_signals"):
        raise HTTPException(400, "mock_signals disabled in config")
    return await signal_svc.create_mock_signal(symbol)
