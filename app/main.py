"""Maverick — single FastAPI process: dashboard + /telegram webhook + /api +
/health + background asyncio watcher (Render free tier, 512MB budget)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app import bootstrap
from app.api.routes import router as api_router
from app.api.telegram_webhook import router as webhook_router
from app.config import get_settings
from app.services import watcher
from app.services.supabase import get_db
from app.services.telegram import get_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("maverick")

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

_schema_status: dict = {"ok": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _schema_status
    _schema_status = await bootstrap.verify()
    task: asyncio.Task | None = None
    if _schema_status["ok"]:
        task = asyncio.create_task(watcher.run(), name="watcher")
    else:
        log.error("watcher NOT started — schema verification failed")
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await get_db().aclose()
    await get_telegram().aclose()


app = FastAPI(title="Maverick", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(api_router)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> JSONResponse:
    """Keep-alive target for the Cloudflare Worker cron."""
    return JSONResponse({
        "status": "ok" if _schema_status.get("ok") else "degraded",
        "schema": _schema_status,
        "watcher": watcher.status(),
        "env": get_settings().app_env,
    })


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html")


@app.get("/logo.svg")
async def logo() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "logo.svg", media_type="image/svg+xml")
