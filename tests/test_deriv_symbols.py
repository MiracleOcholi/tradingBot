"""Symbol resolution: only subscribe to codes Deriv actually offers.

Live symptom this guards against: every candle subscription failed with
`InvalidSymbol`, the socket stayed connected, and streams sat at 0 with no
indication which code was wrong.
"""
import pytest

from app.services.deriv import DerivClient


async def _noop(*a, **k):
    pass


def client(symbols):
    return DerivClient("1089", symbols, _noop)


# Real payload shape: Deriv keys these `underlying_symbol` /
# `underlying_symbol_name`, not `symbol` / `display_name`.
ACTIVE = {
    "active_symbols": [
        {"underlying_symbol": "R_10", "underlying_symbol_name": "Volatility 10 Index",
         "market": "synthetic_index"},
        {"underlying_symbol": "R_50", "underlying_symbol_name": "Volatility 50 Index",
         "market": "synthetic_index"},
        {"underlying_symbol": "JD10", "underlying_symbol_name": "Jump 10 Index",
         "market": "synthetic_index"},
        {"underlying_symbol": "frxEURUSD", "underlying_symbol_name": "EUR/USD",
         "market": "forex"},
    ]
}

LEGACY_ACTIVE = {
    "active_symbols": [
        {"symbol": "R_10", "display_name": "Volatility 10 Index", "market": "synthetic_index"},
    ]
}


async def test_invalid_symbols_are_skipped_valid_ones_kept(monkeypatch):
    c = client(["R_10", "JD75", "R_50", "JD100"])

    async def fake_send(payload, timeout=20.0):
        assert payload["active_symbols"] == "brief"
        return ACTIVE

    monkeypatch.setattr(c, "send", fake_send)
    valid = await c.resolve_symbols()
    assert valid == ["R_10", "R_50"]
    assert c.skipped_symbols == ["JD75", "JD100"]


async def test_available_synthetics_reported_for_diagnosis(monkeypatch):
    c = client(["JD75"])
    monkeypatch.setattr(c, "send", lambda *a, **k: _resolved(ACTIVE))
    await c.resolve_symbols()
    # forex is excluded; synthetic codes are listed with display names
    assert any(s.startswith("JD10 (") for s in c.available_synthetics)
    assert not any("frxEURUSD" in s for s in c.available_synthetics)


async def test_probe_failure_falls_back_to_optimistic_subscribe(monkeypatch):
    c = client(["R_10", "JD75"])

    async def boom(*a, **k):
        raise ConnectionError("socket died")

    monkeypatch.setattr(c, "send", boom)
    valid = await c.resolve_symbols()
    assert valid == ["R_10", "JD75"]      # never blocks the feed on a probe failure
    assert c.skipped_symbols == []


async def _resolved(value):
    return value


@pytest.mark.parametrize("configured,expected", [
    ([], []),
    (["R_10"], ["R_10"]),
    (["NOPE"], []),
])
async def test_edge_cases(monkeypatch, configured, expected):
    c = client(configured)
    monkeypatch.setattr(c, "send", lambda *a, **k: _resolved(ACTIVE))
    assert await c.resolve_symbols() == expected


async def test_legacy_symbol_field_still_accepted(monkeypatch):
    c = client(["R_10", "JD75"])
    monkeypatch.setattr(c, "send", lambda *a, **k: _resolved(LEGACY_ACTIVE))
    assert await c.resolve_symbols() == ["R_10"]
    assert c.skipped_symbols == ["JD75"]


async def test_unparseable_payload_never_skips_everything(monkeypatch):
    """A field-name change must not silently mute the entire feed."""
    c = client(["R_10", "JD10"])
    weird = {"active_symbols": [{"instrument_code": "R_10", "market": "synthetic_index"}]}
    monkeypatch.setattr(c, "send", lambda *a, **k: _resolved(weird))
    assert await c.resolve_symbols() == ["R_10", "JD10"]
    assert c.skipped_symbols == []


async def test_empty_symbol_list_never_mutes_the_feed(monkeypatch):
    """An empty active_symbols response must not skip every symbol —
    subscribing anyway at least surfaces per-symbol errors."""
    c = client(["R_10", "JD10"])
    monkeypatch.setattr(c, "send", lambda *a, **k: _resolved({"active_symbols": []}))
    assert await c.resolve_symbols() == ["R_10", "JD10"]
    assert c.skipped_symbols == []


async def test_probe_request_has_no_product_type(monkeypatch):
    """product_type='basic' returned zero rows on the live socket."""
    seen = {}

    c = client(["R_10"])

    async def capture(payload, timeout=20.0):
        seen.update(payload)
        return ACTIVE

    monkeypatch.setattr(c, "send", capture)
    await c.resolve_symbols()
    assert seen == {"active_symbols": "brief"}
