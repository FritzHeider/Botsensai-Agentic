"""Unit tests for multi-DEX and multi-launchpad normalizer."""

from datetime import UTC, datetime

from botsensai.collectors.multi_dex import (
    RawPoolState,
    normalize_market_snapshot,
    normalize_pool_launch,
)


def test_multi_dex_normalization() -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

    raw_meteora = RawPoolState(
        dex="meteora",
        mint="meteoramint123456789",
        symbol="METEOR",
        name="Meteora Dyn",
        pool_address="pool_addr_123",
        base_reserve=1000000.0,
        quote_reserve_sol=30.0,
        market_cap_usd=65000.0,
        liquidity_usd=12000.0,
        price_usd=0.000065,
        created_at=now,
    )

    launch = normalize_pool_launch(raw_meteora)
    assert launch.token.symbol == "METEOR"
    assert launch.token.mint == "meteoramint123456789"
    assert launch.dev_buy_sol == 30.0

    snap = normalize_market_snapshot(raw_meteora, as_of=now)
    assert snap.market_cap_usd == 65000.0
    assert snap.liquidity_usd == 12000.0
    assert snap.dex == "meteora"
