"""Unit tests for smart money and early buyer tracker."""

from datetime import UTC, datetime

from botsensai.models import Score, Side, TokenRef, Trade
from botsensai.smart_money import discover_smart_money_wallets, render_smart_money_table
from botsensai.store.db import Database


def test_discover_smart_money(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    token = TokenRef(mint="testmint123", name="Test", symbol="TEST")

    # Insert trades for a high conviction wallet
    trades = [
        Trade(
            token=token,
            signature=f"sig_{i}",
            wallet="smart_alpha_wallet_1",
            side=Side.BUY,
            amount_token=1000.0,
            amount_native=2.5,
            as_of=now,
            observed_at=now,
        )
        for i in range(3)
    ]
    db.insert_trades(trades)

    # Insert score so recent_scores finds the token
    db.insert_score(
        Score(
            token=token,
            composite=0.75,
            coverage=0.90,
            as_of=now,
            observed_at=now,
            metric_values=[],
            vetoes=[],
            explanation="High conviction",
            weights_version="v1",
        )
    )

    profiles = discover_smart_money_wallets(db, as_of=now, min_trades=2)
    assert len(profiles) >= 1
    assert profiles[0].wallet == "smart_alpha_wallet_1"
    assert profiles[0].win_rate >= 0.50

    # Smoke render
    render_smart_money_table(profiles)
    db.close()
