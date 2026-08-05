"""Verify-only startup check (API keys cannot run DDL).

Confirms the schema was applied via the Supabase SQL Editor / MCP and the
seed row exists. Never creates or alters anything.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.supabase import get_db

log = logging.getLogger("maverick.bootstrap")

REQUIRED_TABLES = [
    "config", "secrets", "snr_levels", "engine_state",
    "signals", "decisions", "virtual_orders", "trades", "reminders",
]


async def verify() -> dict:
    """Return {ok, missing_tables, seeded}. Logs loud, clear guidance on failure."""
    if not get_settings().supabase_configured:
        log.warning("SUPABASE_URL / SUPABASE_SECRET_KEY not set — running without persistence")
        return {"ok": False, "missing_tables": REQUIRED_TABLES, "seeded": False}

    db = get_db()
    missing: list[str] = []
    for table in REQUIRED_TABLES:
        try:
            await db.select(table, limit=1)
        except Exception:
            missing.append(table)

    seeded = False
    if "config" not in missing:
        try:
            await db.get_config()
            seeded = True
        except Exception:
            pass

    ok = not missing and seeded
    if not ok:
        log.error(
            "SCHEMA CHECK FAILED — missing tables: %s, config seeded: %s. "
            "Apply db/schema.sql then db/seed.sql ONCE via the Supabase SQL Editor.",
            missing or "none", seeded,
        )
    else:
        log.info("schema verified: all %d tables present, config seeded", len(REQUIRED_TABLES))
    return {"ok": ok, "missing_tables": missing, "seeded": seeded}
