"""Tests for point-in-time copytrade wallet skill discovery and metrics."""

from datetime import UTC, datetime

from botsensai.metrics.base import MetricContext
from botsensai.metrics.topology import SmartWalletParticipation
from botsensai.models import Chain, Launch, Launchpad, Side, TokenRef, Trade
from botsensai.onchain.wallet_skill import WalletSkillIndex
from botsensai.store.db import Database


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _make_launch(key: str, created_at: float) -> Launch:
    token = TokenRef(chain=Chain.SOLANA, mint=f"mint_{key}", symbol=key.upper(), name=key)
    return Launch(
        token=token,
        launchpad=Launchpad.PUMPFUN,
        created_at=_dt(created_at),
        observed_at=_dt(created_at),
        source="test",
    )


def test_wallet_skill_look_ahead_boundary() -> None:
    """Assert that trades closed after `before` or `observed_before` do NOT affect the score."""
    db = Database(":memory:")
    launch1 = _make_launch("token1", created_at=100.0)
    launch2 = _make_launch("token2", created_at=300.0)
    db.upsert_launch(launch1)
    db.upsert_launch(launch2)

    wallet = "SkilledTrader11111111111111111111111111111111"

    # Token 1 buy at t=110, sell at t=200 with 5x realized gain (0.1 SOL -> 0.5 SOL)
    db.insert_trades([
        Trade(
            token=launch1.token,
            signature="sig1_buy",
            as_of=_dt(110.0),
            observed_at=_dt(110.0),
            wallet=wallet,
            side=Side.BUY,
            amount_token=1000.0,
            amount_native=0.1,
            source="test",
        ),
        Trade(
            token=launch1.token,
            signature="sig1_sell",
            as_of=_dt(200.0),
            observed_at=_dt(200.0),
            wallet=wallet,
            side=Side.SELL,
            amount_token=1000.0,
            amount_native=0.5,
            source="test",
        ),
    ])

    index = WalletSkillIndex(db)

    # 1. At t=150 (before sell at t=200): position is open, closed_trades = 0 -> skill = 0.5
    res_at_150 = index.skills_for([wallet], before=_dt(150.0), observed_before=_dt(150.0))
    assert res_at_150.scores[wallet] == 0.5

    # 2. At t=250 (after sell at t=200): position is closed with 5x realized PnL -> skill > 0.5
    res_at_250_before_future = index.skills_for([wallet], before=_dt(250.0), observed_before=_dt(250.0))
    skill_at_250 = res_at_250_before_future.scores[wallet]
    assert skill_at_250 > 0.5

    # 3. Add a FUTURE trade at t=400 on Token 2 (disastrous loss)
    db.insert_trades([
        Trade(
            token=launch2.token,
            signature="sig2_buy",
            as_of=_dt(310.0),
            observed_at=_dt(310.0),
            wallet=wallet,
            side=Side.BUY,
            amount_token=5000.0,
            amount_native=1.0,
            source="test",
        ),
        Trade(
            token=launch2.token,
            signature="sig2_sell",
            as_of=_dt(400.0),
            observed_at=_dt(400.0),
            wallet=wallet,
            side=Side.SELL,
            amount_token=5000.0,
            amount_native=0.01,
            source="test",
        ),
    ])

    # Clear cache to force re-evaluation from DB
    index.clear()

    # 4. Re-evaluate as of t=250: future trade at t=400 MUST NOT alter score at t=250!
    res_at_250_after_future = index.skills_for([wallet], before=_dt(250.0), observed_before=_dt(250.0))
    assert res_at_250_after_future.scores[wallet] == skill_at_250

    # 5. Evaluate as of t=500 (after the loss at t=400): score should drop due to the loss
    res_at_500 = index.skills_for([wallet], before=_dt(500.0), observed_before=_dt(500.0))
    assert res_at_500.scores[wallet] < skill_at_250


def test_wallet_skill_scoring_logic() -> None:
    """Verify skill scoring and typical size calculation."""
    db = Database(":memory:")
    launch = _make_launch("token1", created_at=100.0)
    db.upsert_launch(launch)

    skilled = "Skilled111111111111111111111111111111111111"
    unskilled = "Unskilled222222222222222222222222222222222"

    # Skilled: buy 0.2 SOL, sell at 3x
    # Unskilled: buy 0.5 SOL, sell at 0.1x (loss)
    db.insert_trades([
        Trade(
            token=launch.token,
            signature="s_buy1",
            as_of=_dt(105.0),
            observed_at=_dt(105.0),
            wallet=skilled,
            side=Side.BUY,
            amount_token=1000.0,
            amount_native=0.2,
            source="test",
        ),
        Trade(
            token=launch.token,
            signature="s_sell1",
            as_of=_dt(200.0),
            observed_at=_dt(200.0),
            wallet=skilled,
            side=Side.SELL,
            amount_token=1000.0,
            amount_native=0.6,
            source="test",
        ),
        Trade(
            token=launch.token,
            signature="u_buy1",
            as_of=_dt(150.0),
            observed_at=_dt(150.0),
            wallet=unskilled,
            side=Side.BUY,
            amount_token=1000.0,
            amount_native=0.5,
            source="test",
        ),
        Trade(
            token=launch.token,
            signature="u_sell1",
            as_of=_dt(220.0),
            observed_at=_dt(220.0),
            wallet=unskilled,
            side=Side.SELL,
            amount_token=1000.0,
            amount_native=0.05,
            source="test",
        ),
    ])

    index = WalletSkillIndex(db)
    res = index.skills_for([skilled, unskilled], before=_dt(300.0), observed_before=_dt(300.0))

    assert res.scores[skilled] > 0.5
    assert res.scores[unskilled] < 0.5
    assert res.typical_sizes[skilled] == 0.2
    assert res.typical_sizes[unskilled] == 0.5


def test_wallet_skill_knowledge_bound() -> None:
    """Verify that `observed_before` excludes trades observed after the cutoff."""
    db = Database(":memory:")
    launch = _make_launch("token1", created_at=100.0)
    db.upsert_launch(launch)

    wallet = "Trader3333333333333333333333333333333333333"

    # Trade happened at as_of=150, but observed at observed_at=300
    db.insert_trades([
        Trade(
            token=launch.token,
            signature="late_obs_buy",
            as_of=_dt(150.0),
            observed_at=_dt(300.0),
            wallet=wallet,
            side=Side.BUY,
            amount_token=100.0,
            amount_native=0.1,
            source="test",
        ),
        Trade(
            token=launch.token,
            signature="late_obs_sell",
            as_of=_dt(160.0),
            observed_at=_dt(300.0),
            wallet=wallet,
            side=Side.SELL,
            amount_token=100.0,
            amount_native=0.5,
            source="test",
        ),
    ])

    index = WalletSkillIndex(db)

    # Observed before t=200: trade was observed at t=300, so it must be excluded
    res1 = index.skills_for([wallet], before=_dt(250.0), observed_before=_dt(200.0))
    assert res1.scores[wallet] == 0.5
    assert res1.typical_sizes[wallet] == 0.0

    # Observed before t=350: trade observed at t=300, so included
    index.clear()
    res2 = index.skills_for([wallet], before=_dt(250.0), observed_before=_dt(350.0))
    assert res2.scores[wallet] > 0.5
    assert res2.typical_sizes[wallet] == 0.1


def test_smart_wallet_participation_metric_integration() -> None:
    """Verify SmartWalletParticipation metric uses wallet_skill and wallet_typical_size."""
    db = Database(":memory:")
    launch = _make_launch("token1", created_at=100.0)
    db.upsert_launch(launch)

    skilled_wallet = "Skilled44444444444444444444444444444444444"

    # Skilled wallet buys this token
    trade = Trade(
        token=launch.token,
        signature="current_buy",
        as_of=_dt(110.0),
        observed_at=_dt(110.0),
        wallet=skilled_wallet,
        side=Side.BUY,
        amount_token=1000.0,
        amount_native=1.0,
        source="test",
    )

    metric = SmartWalletParticipation()
    ctx = MetricContext(
        token=launch.token,
        as_of=_dt(120.0),
        launch=launch,
        trades=[trade] * 5,  # 5 buys to meet minimum buys requirement
        extra={
            "wallet_skill": {skilled_wallet: 0.85},
            "wallet_typical_size": {skilled_wallet: 1.0},
        },
    )

    val, count, notes = metric.compute(ctx)
    assert val is not None and val > 0.0
    assert "skilled wallets participating" in notes
