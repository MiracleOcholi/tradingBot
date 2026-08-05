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


ACTIVE = {
    "active_symbols": [
        {"symbol": "R_10", "display_name": "Volatility 10 Index", "market": "synthetic_index"},
        {"symbol": "R_50", "display_name": "Volatility 50 Index", "market": "synthetic_index"},
        {"symbol": "JD10", "display_name": "Jump 10 Index", "market": "synthetic_index"},
        {"symbol": "frxEURUSD", "display_name": "EUR/USD", "market": "forex"},
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
