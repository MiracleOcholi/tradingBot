"""Thin async PostgREST client for Supabase using the SECRET key (backend only)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("maverick.supabase")


class SupabaseClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.supabase_url.rstrip("/") + "/rest/v1"
        self._headers = {
            "apikey": s.supabase_secret_key,
            "Authorization": f"Bearer {s.supabase_secret_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- generic helpers -------------------------------------------------
    async def select(self, table: str, query: str = "", limit: int | None = None) -> list[dict]:
        url = f"{self._base}/{table}?{query}" if query else f"{self._base}/{table}"
        if limit:
            url += ("&" if "?" in url else "?") + f"limit={limit}"
        r = await self._client.get(url, headers=self._headers)
        r.raise_for_status()
        return r.json()

    async def insert(self, table: str, row: dict | list[dict]) -> list[dict]:
        r = await self._client.post(
            f"{self._base}/{table}",
            headers={**self._headers, "Prefer": "return=representation"},
            json=row,
        )
        r.raise_for_status()
        return r.json()

    async def update(self, table: str, query: str, patch: dict) -> list[dict]:
        r = await self._client.patch(
            f"{self._base}/{table}?{query}",
            headers={**self._headers, "Prefer": "return=representation"},
            json=patch,
        )
        r.raise_for_status()
        return r.json()

    # ---- config ----------------------------------------------------------
    async def get_config(self) -> dict:
        rows = await self.select("config", "id=eq.1")
        if not rows:
            raise RuntimeError("config seed row missing — run db/seed.sql")
        return rows[0]

    async def update_config(self, patch: dict) -> dict:
        patch = {**patch, "updated_at": "now()"}
        rows = await self.update("config", "id=eq.1", patch)
        return rows[0]

    # ---- signals ----------------------------------------------------------
    async def create_signal(self, row: dict) -> dict:
        return (await self.insert("signals", row))[0]

    async def get_signal(self, signal_id: str) -> dict | None:
        rows = await self.select("signals", f"id=eq.{signal_id}")
        return rows[0] if rows else None

    async def update_signal(self, signal_id: str, patch: dict) -> dict:
        patch = {**patch, "updated_at": "now()"}
        rows = await self.update("signals", f"id=eq.{signal_id}", patch)
        return rows[0]

    async def latest_awaiting_edit(self) -> dict | None:
        rows = await self.select(
            "signals",
            "awaiting_edit_field=not.is.null&status=eq.PENDING&order=updated_at.desc",
            limit=1,
        )
        return rows[0] if rows else None

    async def record_decision(self, signal_id: str, action: str, payload: dict | None = None) -> None:
        await self.insert(
            "decisions",
            {"signal_id": signal_id, "action": action, "payload": payload or {}},
        )


_client: SupabaseClient | None = None


def get_db() -> SupabaseClient:
    global _client
    if _client is None:
        _client = SupabaseClient()
    return _client
