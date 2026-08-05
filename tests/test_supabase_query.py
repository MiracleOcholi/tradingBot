"""Regression: PostgREST query strings must survive URL decoding.

A literal '+' in a query string decodes to a space server-side. An ISO
timestamp filter like `created_at=gte.2026-08-03T02:58:07+00:00` therefore
arrived as `…02:58:07 00:00`, which Postgres rejects — a 400 that aborted
the market boot sequence before the Deriv socket ever opened.
"""
from datetime import UTC, datetime

from app.services.supabase import encode_query


def test_plus_in_iso_timestamp_is_escaped():
    cutoff = datetime(2026, 8, 3, 2, 58, 7, tzinfo=UTC).isoformat()
    assert "+" in cutoff                                  # the hazard
    q = encode_query(f"is_mock=is.false&created_at=gte.{cutoff}")
    assert "+" not in q
    assert "%2B00:00" in q


def test_separators_are_left_intact():
    q = encode_query("symbol=eq.R_10&active=is.true&order=first_candle_at.asc")
    assert q == "symbol=eq.R_10&active=is.true&order=first_candle_at.asc"


def test_in_filter_preserved():
    q = encode_query("status=in.(PENDING,ACCEPTED,FILLED)")
    assert q == "status=in.(PENDING,ACCEPTED,FILLED)"


def test_empty_query_is_safe():
    assert encode_query("") == ""
