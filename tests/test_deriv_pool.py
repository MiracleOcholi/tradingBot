"""Sharding across sockets.

The live API silently accepted only the first 8 subscriptions on a single
connection — 6 symbols × 4 timeframes was requested, R_10 and R_50 arrived
complete, everything else was empty, and no error was raised. The pool
exists so the watchlist is never larger than a connection will carry.
"""
from app.services.deriv_pool import DerivPool, shard_symbols

SYMBOLS = ["R_10", "R_50", "R_75", "JD10", "JD75", "JD100"]
TFS = ["M15", "H1", "H4", "D1"]


async def _noop(*a, **k):
    pass


def pool(symbols=None, max_subs=8):
    return DerivPool("1089", symbols or SYMBOLS, _noop,
                     timeframes=TFS, max_subscriptions=max_subs)


def test_shard_symbols_splits_evenly():
    assert shard_symbols(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert shard_symbols(["a"], 5) == [["a"]]
    assert shard_symbols([], 2) == []


def test_no_shard_exceeds_the_subscription_cap():
    p = pool()
    for shard in p.shards:
        assert len(shard) * len(TFS) <= 8


def test_every_symbol_is_covered_exactly_once():
    p = pool()
    flat = [s for shard in p.shards for s in shard]
    assert sorted(flat) == sorted(SYMBOLS)
    assert len(flat) == len(set(flat))


def test_six_symbols_need_three_connections():
    p = pool()
    assert len(p.clients) == 3          # 2 symbols × 4 tfs = 8 subs each


def test_symbol_routes_to_its_owning_shard():
    p = pool()
    owner = p._client_for("JD100")
    assert "JD100" in owner.symbols
    assert p._client_for("R_10") is not owner


def test_unknown_symbol_falls_back_to_primary():
    p = pool()
    assert p._client_for("NOPE") is p.primary


def test_callbacks_propagate_to_every_shard():
    p = pool()

    async def handler(*a, **k):
        pass

    p.on_tick = handler
    p.on_contract = handler
    assert all(c.on_tick is handler for c in p.clients)
    assert all(c.on_contract is handler for c in p.clients)


def test_status_aggregates_across_shards():
    p = pool()
    p.clients[0].streams[("R_10", "M15")] = object()
    p.clients[1].subscriptions_sent = 8
    p.clients[2].skipped_symbols = ["JD100"]
    s = p.status()
    assert s["connections"] == 3
    assert s["streams"] == 1
    assert s["subscribed"] == 8
    assert s["skipped_symbols"] == ["JD100"]
    assert len(s["shards"]) == 3


def test_tight_cap_yields_one_symbol_per_connection():
    p = pool(max_subs=4)
    assert len(p.clients) == len(SYMBOLS)
    assert all(len(shard) == 1 for shard in p.shards)
