"""Unit tests for token comparison and post-mortem autopsy studio."""

from datetime import UTC, datetime

import pytest

from botsensai.autopsy import autopsy_token, build_autopsy_timeline, compare_tokens
from botsensai.config import Settings
from botsensai.models import Launch, Launchpad, MarketSnapshot, Side, TokenRef, Trade
from botsensai.store.db import Database


def test_build_autopsy_timeline(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    token_key = "solana:testmint123"
    token = TokenRef(mint="testmint123", name="Test", symbol="TEST")

    launch = Launch(token=token, launchpad=Launchpad.PUMPFUN, created_at=now)
    db.upsert_launch(launch)

    # Insert sniper bundle trades
    trades = [
        Trade(
            token=token,
            signature=f"sig{i}",
            wallet=f"sniper_wallet_{i}",
            side=Side.BUY,
            amount_token=1000.0,
            amount_native=2.0,
            as_of=now,
            observed_at=now,
        )
        for i in range(4)
    ]
    # Add deployer dump
    trades.append(
        Trade(
            token=token,
            signature="sig_dump",
            wallet=token.mint,
            side=Side.SELL,
            amount_token=5000.0,
            amount_native=8.0,
            as_of=now,
            observed_at=now,
        )
    )
    db.insert_trades(trades)

    # Add peak snapshot
    db.insert_snapshots(
        [
            MarketSnapshot(
                token=token,
                market_cap_usd=250000.0,
                liquidity_usd=45000.0,
                price_usd=0.00025,
                as_of=now,
                observed_at=now,
            )
        ]
    )

    timeline = build_autopsy_timeline(token_key, db)
    assert len(timeline) >= 3
    categories = [ev.category for ev in timeline]
    assert "launch" in categories
    assert "bundle" in categories
    assert "dump" in categories

    db.close()


@pytest.mark.asyncio
async def test_compare_and_autopsy_smoke(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    # Test that queries execute cleanly without throwing uncaught exceptions
    await compare_tokens("invalid_mint_1", "invalid_mint_2", settings)
    await autopsy_token("invalid_mint_3", settings)
    assert True
