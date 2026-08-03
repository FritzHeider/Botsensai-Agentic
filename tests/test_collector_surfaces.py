"""End-to-end collector contract tests against a canned transport.

`tests/test_collectors.py` covers parsing and the silent-absence guard. What it
does not cover is the part the P0-02 split put at risk: `discover` and `enrich`
must never raise, must still return the records a healthy response produces,
and must set `degraded=True` for a dead sub-surface without losing the parts
that worked.

Every collector here is driven through a `_FakeClient` rather than the network,
so the tests are deterministic and the failure paths are reachable — a route
mapped to an exception raises exactly where the real client would.
"""

from __future__ import annotations

import pytest

from botsensai.collectors.dexscreener import DexscreenerCollector
from botsensai.collectors.geckoterminal import GeckoTerminalCollector
from botsensai.collectors.pumpfun import PumpFunCollector
from botsensai.collectors.social import FourChanBizCollector
from botsensai.models import Chain, TokenRef

MINT = "Mint111111111111111111111111111111111111111"


class _FakeClient:
    """Routes by URL substring. A route whose value is an exception raises it."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def _lookup(self, url: str) -> object:
        self.calls.append(url)
        for fragment, value in self.routes.items():
            if fragment in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise RuntimeError(f"unrouted: {url}")

    async def get_json(self, url, params=None, cache_ttl=None, **kwargs):
        return self._lookup(url)

    async def get_text(self, url, params=None, cache_ttl=None, **kwargs):
        return self._lookup(url)

    async def aclose(self) -> None:
        return None


COIN = {
    "mint": MINT,
    "symbol": "PRB",
    "name": "Probe",
    "created_timestamp": 1751371200000,
    "creator": "Dev11111111111111111111111111111111111111",
    "virtual_sol_reserves": 32e9,
    "real_sol_reserves": 5e9,
    "usd_market_cap": 12345.0,
    "complete": False,
}

TRADE_ROWS = [
    {
        "tx": "sig1",
        "userAddress": "w1",
        "timestamp": "2026-07-01T12:00:00Z",
        "type": "buy",
        "amountSol": "1.5",
        "baseAmount": "1000",
        "slotIndexId": "0004351948800012900000",
    },
    {"signature": "sig2", "trader": "w2", "timestamp": 1751371200000, "side": "S",
     "solAmount": 2.5e9, "tokenAmount": 4e15},
    "not-a-dict",
    {"tx": "sig4", "userAddress": "w4", "timestamp": "not-a-date", "type": "weird"},
]

HOLDERS = {"topHolders": [
    {"address": "h1", "amount": 400, "isDev": True},
    {"address": "h2", "amount": 300, "isSniper": True, "isBundler": True},
    {"wallet": "h4", "amount": 0},
    "not-a-dict",
]}

PAIR_ROWS = [
    {"baseToken": {"address": "a2", "symbol": "AAA"}, "chainId": "solana",
     "pairCreatedAt": 1751371200000, "dexId": "pumpswap", "priceUsd": "0.5",
     "liquidity": {"usd": 1234.5}, "txns": {"m5": {"buys": 3, "sells": "4"}}},
    "not-a-dict",
]

POOL_ROWS = [
    {"attributes": {"name": "BBB / SOL", "pool_created_at": "2026-07-01T12:00:00Z",
                    "base_token_price_usd": "1.5", "reserve_in_usd": "900"},
     "id": "solana_POOLADDR",
     "relationships": {"base_token": {"data": {"id": "solana_MINT1"}},
                       "dex": {"data": {"id": "pump-fun-amm"}}}},
    "not-a-dict",
]


@pytest.fixture
def token() -> TokenRef:
    return TokenRef(chain=Chain.SOLANA, mint=MINT, symbol="PRB", name="Probe")


# --------------------------------------------------------------------------- #
# pump.fun
# --------------------------------------------------------------------------- #


def _pumpfun(*, live: object) -> PumpFunCollector:
    collector = PumpFunCollector()
    collector._client = _FakeClient({
        "sol-price": {"solPrice": 150.0},
        "/coins/currently-live": live,
        "/coins": [COIN, "not-a-dict"],
    })
    return collector


@pytest.mark.asyncio
async def test_pumpfun_discover_reads_listing_and_livestreams():
    live_coin = {**COIN, "mint": "Mint222", "num_participants": 12}
    result = await _pumpfun(live=[live_coin]).discover(limit=5)

    assert not result.degraded
    assert {launch.token.mint for launch in result.launches} == {MINT, "Mint222"}
    assert result.raw["livestreams"]["Mint222"]["num_participants"] == 12


@pytest.mark.asyncio
async def test_pumpfun_discover_keeps_the_listing_when_livestreams_die():
    """A dead sub-surface degrades the result; it does not empty it."""
    result = await _pumpfun(live=RuntimeError("live 500")).discover(limit=5)

    assert result.degraded is True
    assert [launch.token.mint for launch in result.launches] == [MINT]


@pytest.mark.asyncio
async def test_pumpfun_enrich_collects_every_sub_surface(token):
    collector = PumpFunCollector()
    collector._client = _FakeClient({"sol-price": {"solPrice": 150.0}})
    collector._swap = _FakeClient({
        "/trades": {"trades": TRADE_ROWS, "pagination": {"hasMore": False}},
        "market-activity": {"5m": {"volumeUsd": 10, "numBuys": 3, "numSells": "2"}},
    })
    collector._advanced = _FakeClient({"top-holders": HOLDERS})
    collector._livestream = _FakeClient({"livestream": {"id": "s1", "numParticipants": 4,
                                                        "streamStartTimestamp": 1751371200000,
                                                        "title": "live"}})

    result = await collector.enrich([token])

    assert not result.degraded
    assert [t.signature for t in result.trades] == ["sig1", "sig2"]
    assert [h.wallet for h in result.holders] == ["h1", "h2"]
    assert result.security[0].sniper_share == pytest.approx(300 / 700)
    assert result.snapshots and result.posts


@pytest.mark.asyncio
async def test_pumpfun_enrich_never_raises_when_every_host_is_down(token):
    collector = PumpFunCollector()
    collector._client = _FakeClient({"sol-price": RuntimeError("frontend down")})
    for attribute in ("_swap", "_advanced", "_livestream"):
        setattr(collector, attribute, _FakeClient({"": RuntimeError("host down")}))

    result = await collector.enrich([token])

    assert result.degraded is True
    assert result.record_count == 0


# --------------------------------------------------------------------------- #
# dexscreener
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dexscreener_discover_merges_boosts_and_survives_a_dead_feed():
    collector = DexscreenerCollector()
    collector._promo = _FakeClient({
        "token-boosts/latest": [
            {"chainId": "solana", "tokenAddress": "b1", "amount": 10, "totalAmount": 100},
            {"chainId": "nope", "tokenAddress": "b3"},
        ],
        "token-boosts/top": [
            {"chainId": "solana", "tokenAddress": "b1", "amount": 30, "totalAmount": 60},
        ],
        "token-profiles": [{"p": 1}, "not-a-dict"],
        "metas/trending": RuntimeError("metas down"),
        "community-takeovers": [{"c": 1}],
    })
    collector._client = _FakeClient({"/tokens/v1/": PAIR_ROWS})

    result = await collector.discover(limit=5)

    # The two ledgers fold into one record holding the larger of each figure.
    assert result.raw["boosts"] == {
        "solana:b1": {"chainId": "solana", "tokenAddress": "b1", "amount": 30.0,
                      "totalAmount": 100.0, "description": None, "links": None}
    }
    assert result.raw["profiles"] == [{"p": 1}]
    # A dead metas feed is commentary, not impairment: no degrade, no key.
    assert "metas" not in result.raw
    assert not result.degraded
    assert len(result.snapshots) == 1


@pytest.mark.asyncio
async def test_dexscreener_enrich_degrades_on_pairs_but_not_on_orders(token):
    collector = DexscreenerCollector()
    collector._promo = _FakeClient({"/orders/": RuntimeError("orders 404")})
    collector._client = _FakeClient({"/tokens/v1/": PAIR_ROWS})

    ok = await collector.enrich([token])
    assert not ok.degraded
    assert len(ok.snapshots) == 1

    collector._client = _FakeClient({"/tokens/v1/": RuntimeError("pairs 500")})
    broken = await collector.enrich([token])
    assert broken.degraded is True
    assert broken.record_count == 0


# --------------------------------------------------------------------------- #
# geckoterminal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_geckoterminal_enrich_reads_multi_and_info(token):
    collector = GeckoTerminalCollector()
    collector._client = _FakeClient({
        "tokens/multi": {"data": ["bad", {}, {"attributes": {"address": "m1", "symbol": "M",
                                                             "price_usd": "2"}}]},
        "/info": {"data": {"attributes": {"gt_score": 55.5, "mint_authority": None,
                                          "freeze_authority": "revoked"}}},
    })

    result = await collector.enrich([token])

    assert not result.degraded
    assert [s.token.mint for s in result.snapshots] == ["m1"]
    assert result.raw["gt_scores"][token.key]["gt_score"] == 55.5
    assert result.security


@pytest.mark.asyncio
async def test_geckoterminal_discover_survives_dead_pool_feeds():
    collector = GeckoTerminalCollector()
    collector._client = _FakeClient({
        "new_pools": {"data": POOL_ROWS},
        "trending_pools": RuntimeError("trending 500"),
    })

    result = await collector.discover()

    assert result.degraded is True
    assert len(result.launches) == 1
    assert collector._pool_to_records(POOL_ROWS[0])[0].launchpad.value == "pumpswap"


# --------------------------------------------------------------------------- #
# 4chan
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fourchan_enrich_attributes_by_ticker_and_leaves_addresses_untagged(token):
    collector = FourChanBizCollector()
    collector._client = _FakeClient({
        "catalog.json": [
            {"threads": [{"no": 1, "sub": "$WIF to the moon"}, {"no": 2, "com": "quiet"},
                         "not-a-dict"]},
            {"threads": [{"no": 3, "com": "BONK thread"}]},
        ],
        "/thread/1.json": {"posts": [
            {"no": 1, "resto": 0, "time": 1751371200, "com": "$WIF is the play"},
            {"no": 2, "resto": 1, "time": 1751371260, "com": f"mint {MINT}"},
            {"no": 3, "resto": 1, "time": 1751371320, "com": "unrelated chatter"},
        ]},
        "/thread/3.json": RuntimeError("thread 404"),
    })
    wif = TokenRef(chain=Chain.SOLANA, mint="M3", symbol="WIF")
    bonk = TokenRef(chain=Chain.SOLANA, mint="M2", symbol="BONK")

    result = await collector.enrich([token, bonk, wif])

    assert result.degraded is True  # thread 3 died
    by_id = {post.post_id: post for post in result.posts}
    assert set(by_id) == {"1", "2"}  # the unrelated post is dropped
    assert by_id["1"].token_key == wif.key
    # Matched only by mint address: attributing it to a token would be a guess.
    assert by_id["2"].token_key is None


@pytest.mark.asyncio
async def test_fourchan_enrich_returns_empty_when_the_catalog_is_down(token):
    collector = FourChanBizCollector()
    collector._client = _FakeClient({"catalog.json": RuntimeError("4chan 503")})

    result = await collector.enrich([token])

    assert result.degraded is True
    assert result.posts == []
