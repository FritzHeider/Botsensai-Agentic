"""Tests for CopyTradeTracker top trader discovery and alert generation."""

from datetime import datetime, timezone
import pytest

from botsensai.copytrade.tracker import CopyTradeTracker
from botsensai.models import Side, TokenRef, Trade
from botsensai.store.db import Database


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_copytrade.db"
    db = Database(db_file)
    try:
        yield db
    finally:
        db.close()


def test_copytrade_init_tables(temp_db):
    tracker = CopyTradeTracker(temp_db)
    with temp_db.conn as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "top_traders" in tables
        assert "copy_trade_events" in tables


def test_copytrade_refresh_and_alert(temp_db):
    tracker = CopyTradeTracker(temp_db, min_skill=0.50, min_win_rate=0.40, min_trades=3)
    
    # Insert trades for an elite trader who consistently buys early and sells at 2x
    elite_wallet = "EliteWinnerWallet1111111111111111111111111"
    
    now = datetime.now(timezone.utc)
    trades = []
    for i in range(5):
        # Buy trade
        trades.append(
            Trade(
                token=TokenRef(chain="solana", mint=f"token_{i}"),
                as_of=now,
                side=Side.BUY,
                amount_token=1000.0,
                amount_native=1.0,
                price_native=0.001,
                signature=f"sig_buy_{i}",
                wallet=elite_wallet,
            )
        )
        # Profitable Sell trade (2x)
        trades.append(
            Trade(
                token=TokenRef(chain="solana", mint=f"token_{i}"),
                as_of=now,
                side=Side.SELL,
                amount_token=1000.0,
                amount_native=2.0,
                price_native=0.002,
                signature=f"sig_sell_{i}",
                wallet=elite_wallet,
            )
        )
    temp_db.insert_trades(trades)

    # Refresh top traders roster
    profiles = tracker.refresh_top_traders(max_candidates=10)
    assert len(profiles) >= 1
    assert profiles[0].wallet == elite_wallet
    assert profiles[0].win_rate >= 0.8
    assert profiles[0].skill_score >= 0.50

    # Verify get_top_traders
    traders = tracker.get_top_traders(limit=10)
    assert len(traders) >= 1
    assert traders[0]["wallet"] == elite_wallet

    # Check copy trade alert generation on a new token launch
    launch_time = now.timestamp()
    new_token_trades = [
        Trade(
            token=TokenRef(chain="solana", mint="new_runner_token"),
            as_of=now,
            side=Side.BUY,
            amount_token=500.0,
            amount_native=1.5,
            price_native=0.003,
            signature="sig_copy_entry",
            wallet=elite_wallet,
        )
    ]

    alerts = tracker.check_copy_trade(
        token_key="solana:new_runner_token",
        symbol="RUNNER",
        mint="new_runner_token",
        launch_created_at=launch_time - 30,  # 30 seconds after launch
        trades=new_token_trades,
    )

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.trader_wallet == elite_wallet
    assert alert.conviction_boost > 0.10
    assert alert.recommended_size_sol > 0.01

    # Record copy event
    event_id = tracker.record_copy_event(alert)
    assert event_id > 0

    with temp_db.conn as conn:
        saved = conn.execute("SELECT * FROM copy_trade_events WHERE id = ?", (event_id,)).fetchone()
        assert saved is not None
