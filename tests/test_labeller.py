"""Tests for the outcome labeller behind `botsensai label`.

The label is the training target for everything in phases 3 and 4, so a wrong
label is not a wrong number — it is a scorer taught to want the wrong thing. The
specific failure this module exists to prevent has a name: **peak-price
fantasy**. A token that printed 40x on two hundred dollars of liquidity gave
nobody 40x, and a label that claims otherwise teaches the system to hunt for
exactly those tokens.

The rest of these tests are about the other half of honesty — refusing to claim
a number that is not there. No t0 price means no multiple, not 1.0. A path that
stops at four hours says nothing about survival at 24, and must not be recorded
as a death.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from botsensai.config import Settings
from botsensai.labeller import (
    LABEL_SURFACE,
    LabelPolicy,
    OutcomeLabeller,
    PricePoint,
    choose_denomination,
    collapse_path,
    label_from_points,
    merge_points,
    points_from_candles,
    points_from_snapshots,
)
from botsensai.models import (
    Chain,
    CurveStage,
    Launch,
    Launchpad,
    MarketSnapshot,
    TokenRef,
)
from botsensai.store.db import Database

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _token(mint: str = "MINT111") -> TokenRef:
    return TokenRef(chain=Chain.SOLANA, mint=mint, symbol="TEST")


def _launch(created_at: datetime = T0, mint: str = "MINT111") -> Launch:
    return Launch(
        token=_token(mint),
        launchpad=Launchpad.PUMPFUN,
        deployer="DEV1",
        created_at=created_at,
        observed_at=created_at,
        source="pumpfun",
    )


def _point(
    offset_seconds: float,
    price: float,
    *,
    high: float | None = None,
    liquidity_usd: float | None = None,
    stage: CurveStage = CurveStage.GRADUATED,
    market_cap_usd: float | None = None,
) -> PricePoint:
    return PricePoint(
        as_of=T0 + timedelta(seconds=offset_seconds),
        price=price,
        high=high if high is not None else price,
        liquidity_usd=liquidity_usd,
        market_cap_usd=market_cap_usd,
        stage=stage,
        source="test",
    )


def _snapshot(
    offset_seconds: float,
    *,
    price_usd: float | None = None,
    price_native: float | None = None,
    liquidity_usd: float | None = None,
    stage: CurveStage = CurveStage.GRADUATED,
    pair_address: str | None = None,
    source: str = "dexscreener",
    mint: str = "MINT111",
) -> MarketSnapshot:
    stamp = T0 + timedelta(seconds=offset_seconds)
    return MarketSnapshot(
        token=_token(mint),
        as_of=stamp,
        observed_at=stamp,
        stage=stage,
        price_usd=price_usd,
        price_native=price_native,
        liquidity_usd=liquidity_usd,
        pair_address=pair_address,
        source=source,
    )


# --------------------------------------------------------------------------- #
# The one the plan asks for: the peak is not an exit
# --------------------------------------------------------------------------- #


def test_a_spike_on_thin_liquidity_is_not_realizable():
    """The acceptance condition for P1-03.

    A token whose price went 10x on 200 USD of liquidity. The chart says 10x.
    Selling a 0.25 SOL position into a pool that shallow moves the price most of
    the way back down before the order finishes, so the realizable multiple has
    to come out materially lower. If these two numbers are ever close on a token
    this thin, the depth model has stopped being consulted.
    """
    policy = LabelPolicy(position_size_native=0.25, native_price_usd=150.0)
    launch = _launch()
    points = [
        _point(0, 1e-6, liquidity_usd=200.0),
        _point(600, 1e-5, liquidity_usd=200.0),  # 10x on the chart
        _point(1200, 2e-6, liquidity_usd=200.0),
    ]

    outcome = label_from_points(launch, points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.max_multiple_from_t0 is not None
    assert outcome.max_realizable_multiple is not None
    assert abs(outcome.max_multiple_from_t0 - 10.0) < 1e-6
    assert outcome.max_realizable_multiple < outcome.max_multiple_from_t0 * 0.5, (
        "a 10x on 200 USD of liquidity that labels as anything close to 10x is "
        "peak-price fantasy, which is the exact bias this field exists to price"
    )
    # And it is not merely smaller — it is bounded by the pool. Total depth is
    # 200 USD ≈ 1.33 SOL, so a 0.25 SOL position cannot possibly return 10x.
    assert outcome.max_realizable_multiple < (200.0 / 150.0) / 0.25


def test_deep_liquidity_lets_most_of_the_move_through():
    """The control for the test above: the tax is depth-dependent, not a constant.

    Same 10x, same position, two million dollars of depth instead of two
    hundred. If the realizable figure were a fixed haircut rather than a curve,
    this would come out the same as the thin case.
    """
    policy = LabelPolicy(position_size_native=0.25, native_price_usd=150.0)
    points = [
        _point(0, 1e-6, liquidity_usd=2_000_000.0),
        _point(600, 1e-5, liquidity_usd=2_000_000.0),
        _point(1200, 2e-6, liquidity_usd=2_000_000.0),
    ]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.max_realizable_multiple is not None
    assert outcome.max_realizable_multiple > 9.0, "deep pools should pass most of the move through"
    assert outcome.max_realizable_multiple <= outcome.max_multiple_from_t0


def test_realizable_never_exceeds_the_peak_multiple():
    """An exit that beats the chart is arithmetically impossible.

    Entry is priced at t0 spot and exits are charged impact, so the realizable
    figure is bounded above by the raw ratio. A violation means a sign error or
    a curve reconstructed from the wrong price.
    """
    policy = LabelPolicy(position_size_native=0.25)
    for liquidity in (150.0, 1_000.0, 50_000.0, 5_000_000.0):
        points = [
            _point(0, 2e-7, liquidity_usd=liquidity),
            _point(300, 6e-6, liquidity_usd=liquidity),
            _point(900, 1e-6, liquidity_usd=liquidity),
        ]
        outcome = label_from_points(_launch(), points, policy, denomination="usd")
        assert outcome is not None
        assert outcome.max_realizable_multiple is not None
        assert outcome.max_multiple_from_t0 is not None
        assert outcome.max_realizable_multiple <= outcome.max_multiple_from_t0 + 1e-9, liquidity


def test_the_best_exit_is_not_always_the_peak_point():
    """Depth and price do not peak together, and the label follows the money.

    Here the price high sits on a pool with almost nothing in it, while a
    slightly lower price later sits on a deep one. The realizable exit is the
    second, and a labeller that only ever evaluated the peak point would report
    the worse of the two.
    """
    policy = LabelPolicy(position_size_native=0.25, native_price_usd=150.0)
    points = [
        _point(0, 1e-6, liquidity_usd=5_000.0),
        _point(300, 1e-5, liquidity_usd=150.0),  # the high, on nothing
        _point(600, 8e-6, liquidity_usd=400_000.0),  # slightly lower, deep
    ]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.max_realizable_multiple is not None
    assert outcome.max_realizable_multiple > 7.0, (
        "the deep 8x should be reachable even though the 10x was not"
    )


# --------------------------------------------------------------------------- #
# Refusing to claim what is not known
# --------------------------------------------------------------------------- #


def test_a_late_first_observation_yields_no_multiple_rather_than_one():
    """No t0 price means no multiple.

    A launch first seen two hours after the mint has no t0 price on record. The
    tempting default is 1.0 — "it did nothing" — which is a confident bearish
    claim about a token nobody was watching, and it lands in the training set as
    a real label.
    """
    policy = LabelPolicy(t0_lag_tolerance_seconds=900.0)
    points = [_point(7200, 3e-6, liquidity_usd=9_000.0), _point(9000, 5e-6, liquidity_usd=9_000.0)]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.max_multiple_from_t0 is None
    assert outcome.max_realizable_multiple is None
    assert outcome.peak_at == T0 + timedelta(seconds=9000), "the peak is still known and recorded"


def test_survival_is_only_claimed_over_horizons_the_path_reaches():
    """A path that stops at four hours says nothing about hour 24.

    `survived_24h` defaults to False, and the difference between "it died" and
    "we stopped watching" is the entire value of the label. The flag stays False
    here, but only because it is unclaimed — the 1h flag, which the path does
    cover, is True.
    """
    policy = LabelPolicy()
    points = [
        _point(0, 1e-6, liquidity_usd=9_000.0),
        _point(3600, 2e-6, liquidity_usd=9_000.0),
        _point(4 * 3600, 3e-6, liquidity_usd=9_000.0),
    ]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.survived_1h is True
    assert outcome.survived_24h is False
    assert outcome.survived_7d is False


def test_survival_is_evaluated_at_the_horizon_not_at_the_end():
    """A token that was alive at 1h and dead at 24h survived the first, not the second.

    The check reads the last price at or before each deadline. Using the final
    price for every horizon would mark this token dead at 1h too, erasing the
    only part of its life a fast strategy could have traded.
    """
    policy = LabelPolicy(survival_fraction=0.5)
    points = [
        _point(0, 1e-6, liquidity_usd=9_000.0),
        _point(3000, 4e-6, liquidity_usd=9_000.0),
        _point(25 * 3600, 1e-8, liquidity_usd=9_000.0),
    ]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.survived_1h is True
    assert outcome.survived_24h is False


def test_a_price_stamped_before_the_mint_does_not_void_the_multiple():
    """Caught live: adding candles *lost* two labels that snapshots alone had.

    An OHLCV candle is stamped with the start of its bucket, so the candle
    containing a mint begins before it, and a pool that predates our recorded
    `created_at` contributes older ones still. Those points were becoming the
    path's first point, pushing the apparent t0 lag negative and voiding the
    multiple on a token whose t0 we genuinely had.
    """
    policy = LabelPolicy(path_bucket_seconds=30.0)
    points = [
        _point(-7200, 5e-7, liquidity_usd=5_000.0),  # another pool's history
        _point(-20, 1e-6, liquidity_usd=5_000.0),  # the candle containing the mint
        _point(600, 4e-6, liquidity_usd=5_000.0),
    ]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.max_multiple_from_t0 is not None
    assert abs(outcome.max_multiple_from_t0 - 4.0) < 1e-9, (
        "t0 is the candle containing the mint, not the two-hour-old one"
    )


def test_an_empty_path_produces_no_label_at_all():
    """No price is not a zero-multiple outcome; it is an absent one."""
    assert label_from_points(_launch(), [], LabelPolicy(), denomination="usd") is None


# --------------------------------------------------------------------------- #
# Rugs, graduation, denomination
# --------------------------------------------------------------------------- #


def test_a_price_fade_is_not_labelled_a_rug():
    """Deployer vetoes fire at one rug, so a fade must not count as one.

    `Database.deployer_history` feeds `veto_deployer_rug_count = 1`. This token
    lost 99% of its value with its liquidity intact — a dead token, not a
    stolen one — and mislabelling it bans its deployer forever.
    """
    points = [
        _point(0, 1e-6, liquidity_usd=20_000.0),
        _point(600, 5e-6, liquidity_usd=22_000.0),
        _point(9000, 1e-8, liquidity_usd=18_000.0),
    ]
    outcome = label_from_points(_launch(), points, LabelPolicy(), denomination="usd")
    assert outcome is not None
    assert outcome.rugged is False


def test_a_liquidity_withdrawal_is_labelled_a_rug():
    """The signature of a rug is the pool emptying, and that is what is checked."""
    points = [
        _point(0, 1e-6, liquidity_usd=20_000.0),
        _point(600, 5e-6, liquidity_usd=22_000.0),
        _point(9000, 1e-8, liquidity_usd=40.0),
    ]
    outcome = label_from_points(_launch(), points, LabelPolicy(), denomination="usd")
    assert outcome is not None
    assert outcome.rugged is True


def test_a_token_that_never_had_liquidity_is_not_a_rug():
    """Nothing was withdrawn, because there was never anything to withdraw."""
    points = [
        _point(0, 1e-6, liquidity_usd=60.0),
        _point(600, 2e-6, liquidity_usd=40.0),
        _point(9000, 1e-8, liquidity_usd=20.0),
    ]
    outcome = label_from_points(_launch(), points, LabelPolicy(), denomination="usd")
    assert outcome is not None
    assert outcome.rugged is False


def test_graduation_is_taken_from_the_first_point_that_shows_it():
    policy = LabelPolicy()
    points = [
        _point(0, 1e-6, liquidity_usd=5_000.0, stage=CurveStage.BONDING),
        _point(1800, 2e-6, liquidity_usd=30_000.0, stage=CurveStage.GRADUATED),
        _point(3600, 3e-6, liquidity_usd=40_000.0, stage=CurveStage.GRADUATED),
    ]
    outcome = label_from_points(_launch(), points, policy, denomination="usd")
    assert outcome is not None
    assert outcome.graduated is True
    assert outcome.graduated_at == T0 + timedelta(seconds=1800)


def test_market_cap_is_reported_only_where_a_source_published_one():
    """No supply figure is on a price point, so no cap is invented from one."""
    points = [
        _point(0, 1e-6, liquidity_usd=5_000.0),
        _point(600, 5e-6, liquidity_usd=5_000.0, market_cap_usd=90_000.0),
        _point(1200, 2e-6, liquidity_usd=5_000.0, market_cap_usd=30_000.0),
    ]
    outcome = label_from_points(_launch(), points, LabelPolicy(), denomination="usd")
    assert outcome is not None
    assert outcome.peak_market_cap_usd == 90_000.0
    assert outcome.final_market_cap_usd == 30_000.0

    bare = label_from_points(
        _launch(), [_point(0, 1e-6), _point(600, 5e-6)], LabelPolicy(), denomination="usd"
    )
    assert bare is not None
    assert bare.peak_market_cap_usd is None


def test_denomination_is_chosen_once_and_never_mixed():
    """A path that mixes SOL and USD turns a move in SOL into apparent alpha."""
    native_only = [
        _snapshot(0, price_native=2e-8, source="pumpfun_ws"),
        _snapshot(60, price_native=4e-8, source="pumpfun_ws"),
    ]
    assert choose_denomination(native_only) == "native"
    assert len(points_from_snapshots(native_only, "native")) == 2
    assert points_from_snapshots(native_only, "usd") == []

    usd_heavy = [
        _snapshot(0, price_usd=1e-6, price_native=2e-8),
        _snapshot(60, price_usd=2e-6),
        _snapshot(120, price_usd=3e-6),
    ]
    assert choose_denomination(usd_heavy) == "usd"


def test_a_sol_only_websocket_token_is_still_labelled():
    """The pumpportal frame carries no USD anywhere, and it holds the real t0.

    Those rows are the only genuine bonding-curve price at the instant of mint.
    A labeller that reached for `price_usd` would silently skip exactly the
    tokens whose t0 is trustworthy.
    """
    snapshots = [
        _snapshot(0, price_native=2e-8, stage=CurveStage.BONDING, source="pumpfun_ws"),
        _snapshot(600, price_native=8e-8, stage=CurveStage.BONDING, source="pumpfun"),
    ]
    denomination = choose_denomination(snapshots)
    outcome = label_from_points(
        _launch(), points_from_snapshots(snapshots, denomination), LabelPolicy(), denomination
    )
    assert denomination == "native"
    assert outcome is not None
    assert outcome.max_multiple_from_t0 is not None
    assert abs(outcome.max_multiple_from_t0 - 4.0) < 1e-9


# --------------------------------------------------------------------------- #
# Simultaneous observations are one observation, not a price move
# --------------------------------------------------------------------------- #


def test_sources_disagreeing_in_one_sweep_do_not_become_a_price_move():
    """The bug this guard was written for, reproduced from the real store.

    Two GeckoTerminal rows nine *microseconds* apart quoted 2.05e-6 and 0.307
    USD for the same token. Walked in order that is a 149,878x gain, and it was
    the largest label in the store until the path was collapsed. Every collector
    in a sweep writes at once, so those rows are three sources quoting one
    moment — a disagreement, not a move.
    """
    policy = LabelPolicy(path_bucket_seconds=30.0)
    points = [
        _point(0.0, 2.051291520722583e-06, liquidity_usd=0.0),
        _point(0.000009, 0.30744437565637595, liquidity_usd=94_069.53),
    ]

    outcome = label_from_points(_launch(), points, policy, denomination="usd")

    assert outcome is not None
    assert outcome.max_multiple_from_t0 == 1.0, (
        "a 149,878x that happened in nine microseconds is a data-quality "
        "artifact and must not reach the training target"
    )


def test_collapsing_keeps_moves_that_are_further_apart_than_the_bucket():
    """The control: the guard must not flatten genuine multi-minute paths.

    Measured on the real store, the moves it has to preserve are things like a
    9.2x over 52 minutes. Bucketing by gap rather than by a fixed grid is what
    keeps a sweep together without merging two of them.
    """
    points = [
        _point(0, 1e-6, liquidity_usd=5_000.0),
        _point(5, 1.1e-6, liquidity_usd=5_000.0),
        _point(3120, 9e-6, liquidity_usd=16_000.0),
    ]

    collapsed = collapse_path(points, bucket_seconds=30.0)

    assert len(collapsed) == 2
    outcome = label_from_points(_launch(), points, LabelPolicy(), denomination="usd")
    assert outcome is not None
    assert outcome.max_multiple_from_t0 is not None
    assert outcome.max_multiple_from_t0 > 8.0


def test_a_bucket_takes_the_most_advanced_stage_and_the_median_depth():
    """A source that has not noticed a graduation yet is stale, not contradictory."""
    points = [
        _point(0, 1e-6, liquidity_usd=1_000.0, stage=CurveStage.BONDING),
        _point(2, 2e-6, liquidity_usd=5_000.0, stage=CurveStage.GRADUATED),
        _point(4, 3e-6, liquidity_usd=9_000.0, stage=CurveStage.BONDING),
    ]

    collapsed = collapse_path(points, bucket_seconds=30.0)

    assert len(collapsed) == 1
    assert collapsed[0].stage is CurveStage.GRADUATED
    assert collapsed[0].price == 2e-6
    assert collapsed[0].liquidity_usd == 5_000.0


def test_a_terminal_stage_outranks_graduation_inside_a_bucket():
    """RUGGED and DEAD are terminal, so they win over a stale `graduated`."""
    points = [
        _point(0, 1e-6, liquidity_usd=1_000.0, stage=CurveStage.GRADUATED),
        _point(3, 1e-9, liquidity_usd=5.0, stage=CurveStage.RUGGED),
    ]

    collapsed = collapse_path(points, bucket_seconds=30.0)

    assert len(collapsed) == 1
    assert collapsed[0].stage is CurveStage.RUGGED


# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #


def test_candles_are_sorted_deduplicated_and_keep_their_highs():
    """GeckoTerminal returns candles newest-first, sometimes twice.

    Verified live 2026-07-31 against a real pool: the same timestamp came back
    on two consecutive rows. Sorting and de-duplication happen here so no caller
    has to remember, and the candle's `high` is kept because a minute of price
    action is otherwise invisible.
    """
    candles = [
        {"timestamp": 180, "open": 3.0, "high": 9.0, "low": 3.0, "close": 4.0, "volume": 1.0},
        {"timestamp": 120, "open": 2.0, "high": 2.5, "low": 2.0, "close": 3.0, "volume": 1.0},
        {"timestamp": 180, "open": 3.0, "high": 5.0, "low": 3.0, "close": 4.0, "volume": 1.0},
        {"timestamp": 60, "open": 1.0, "high": 1.2, "low": 1.0, "close": 2.0, "volume": 1.0},
        {"timestamp": 240, "open": 4.0, "high": 4.0, "low": 0.0, "close": None, "volume": 0.0},
    ]

    points = points_from_candles(candles, liquidity_usd=1_000.0)

    assert [int(p.as_of.timestamp()) for p in points] == [60, 120, 180]
    assert points[-1].high == 9.0, "the taller of two rows on the same minute must win"
    assert points[-1].price == 4.0
    assert all(p.liquidity_usd == 1_000.0 for p in points)


def test_a_candle_is_not_evidence_of_graduation():
    """GeckoTerminal indexes bonding-curve pools too.

    Defaulting candles to `GRADUATED` would have marked every OHLCV-extended
    token graduated whether it was or not — and `graduated` is a label the
    regime estimator and the fitter both read.
    """
    points = points_from_candles([{"timestamp": 60, "high": 2.0, "close": 2.0}])
    assert points[0].stage is CurveStage.BONDING

    inherited = points_from_candles(
        [{"timestamp": 60, "high": 2.0, "close": 2.0}], stage=CurveStage.GRADUATED
    )
    assert inherited[0].stage is CurveStage.GRADUATED


def test_merging_prefers_a_snapshot_over_a_candle_at_the_same_instant():
    """A snapshot carries real depth and a real stage; a candle carries neither."""
    snapshot_point = _point(60, 5.0, liquidity_usd=1_234.0)
    candle_points = points_from_candles(
        [{"timestamp": int((T0 + timedelta(seconds=60)).timestamp()), "high": 6.0, "close": 5.5}],
        liquidity_usd=None,
    )

    merged = merge_points(candle_points, [snapshot_point])

    assert len(merged) == 1
    assert merged[0].liquidity_usd == 1_234.0


def test_candles_extend_a_path_that_stored_snapshots_cannot_reach(tmp_path):
    """The reason OHLCV is in this task at all.

    Measured on the real store 2026-07-31: the median stored path spans zero
    minutes and only 12 launches of 651 span a full day. Without candles almost
    every label would be computed from a single point, which is not a path.
    """
    settings = Settings()
    db = Database(str(tmp_path / "label.db"))
    launch = _launch()
    db.upsert_launch(launch)
    db.insert_snapshots(
        [_snapshot(60, price_usd=1e-6, liquidity_usd=5_000.0, pair_address="POOL1")]
    )

    class FakeGecko:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def ohlcv(self, pool, timeframe="minute", aggregate=1, limit=100, before_timestamp=None):
            self.calls.append(
                {"pool": pool, "timeframe": timeframe, "before_timestamp": before_timestamp}
            )
            base = int(T0.timestamp())
            return [
                {"timestamp": base + 7200, "high": 9e-6, "close": 8e-6},
                {"timestamp": base + 3600, "high": 4e-6, "close": 3e-6},
            ]

    gecko = FakeGecko()
    labeller = OutcomeLabeller(settings, db=db, policy=LabelPolicy(), gecko=gecko)
    stats = _run(labeller.run(use_ohlcv=True))

    assert stats.labelled == 1
    assert stats.ohlcv_fetched == 1
    assert {c["timeframe"] for c in gecko.calls} == {"minute", "hour"}
    assert all(c["before_timestamp"] > int(T0.timestamp()) for c in gecko.calls), (
        "without before_timestamp the API returns the newest candles, which for "
        "a token that died on day one is the wrong end of its life"
    )

    outcome = db.outcome(launch.token.key)
    assert outcome is not None
    assert outcome.peak_at is not None
    assert outcome.peak_at.timestamp() == T0.timestamp() + 7200
    db.close()


# --------------------------------------------------------------------------- #
# The pass over the store
# --------------------------------------------------------------------------- #


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _aged_launch(db: Database, mint: str, age_hours: float) -> Launch:
    from botsensai.models import utcnow

    created = utcnow() - timedelta(hours=age_hours)
    launch = Launch(
        token=_token(mint),
        launchpad=Launchpad.PUMPFUN,
        deployer="DEV1",
        created_at=created,
        observed_at=created,
        source="pumpfun",
    )
    db.upsert_launch(launch)
    for offset, price in ((60.0, 1e-6), (900.0, 3e-6)):
        stamp = created + timedelta(seconds=offset)
        db.insert_snapshots(
            [
                MarketSnapshot(
                    token=launch.token,
                    as_of=stamp,
                    observed_at=stamp,
                    stage=CurveStage.GRADUATED,
                    price_usd=price,
                    liquidity_usd=8_000.0,
                    source="dexscreener",
                )
            ]
        )
    return launch


def test_only_launches_past_the_age_cutoff_are_labelled(tmp_path):
    """A token labelled at four hours old is labelled before its life happened."""
    db = Database(str(tmp_path / "pending.db"))
    old = _aged_launch(db, "OLDMINT1", age_hours=48.0)
    young = _aged_launch(db, "NEWMINT1", age_hours=2.0)

    labeller = OutcomeLabeller(Settings(), db=db, policy=LabelPolicy(min_age_hours=24.0))
    stats = _run(labeller.run(use_ohlcv=False))

    assert stats.considered == 1
    assert stats.labelled == 1
    assert db.outcome(old.token.key) is not None
    assert db.outcome(young.token.key) is None
    db.close()


def test_an_already_labelled_launch_is_skipped_unless_refreshed(tmp_path):
    db = Database(str(tmp_path / "refresh.db"))
    _aged_launch(db, "OLDMINT1", age_hours=48.0)

    first = _run(OutcomeLabeller(Settings(), db=db, policy=LabelPolicy()).run(use_ohlcv=False))
    assert first.labelled == 1

    again = _run(OutcomeLabeller(Settings(), db=db, policy=LabelPolicy()).run(use_ohlcv=False))
    assert again.considered == 0

    forced = _run(
        OutcomeLabeller(Settings(), db=db, policy=LabelPolicy()).run(use_ohlcv=False, refresh=True)
    )
    assert forced.labelled == 1
    db.close()


def test_a_pass_writes_a_heartbeat_even_when_it_labels_nothing(tmp_path):
    """Same rule as the sweep loop: a pass that left no row is an invisible gap."""
    db = Database(str(tmp_path / "beat.db"))
    stats = _run(OutcomeLabeller(Settings(), db=db, policy=LabelPolicy()).run(use_ohlcv=False))

    assert stats.considered == 0
    runs = [r for r in db.recent_runs(limit=20) if r["surface"] == LABEL_SURFACE]
    assert len(runs) == 1
    assert runs[0]["ok"] is True
    db.close()


def test_an_interrupted_pass_still_leaves_a_heartbeat(tmp_path):
    """Caught by a real SIGINT against the CLI, not by reading.

    `asyncio.run` cancels the main task rather than raising KeyboardInterrupt
    inside it, so the cancellation surfaced as a `CancelledError` out of the
    loop and skipped `record_run` entirely. An interrupted pass that leaves no
    row is indistinguishable afterwards from one that never started — the same
    failure the sweep loop's heartbeat exists to prevent.
    """
    db = Database(str(tmp_path / "interrupt.db"))
    for index in range(3):
        _aged_launch(db, f"STOPPED{index}", age_hours=48.0 + index)

    labeller = OutcomeLabeller(Settings(), db=db, policy=LabelPolicy())
    original = labeller.label_one
    seen = 0

    async def stop_after_one(launch, **kw):
        nonlocal seen
        seen += 1
        if seen > 1:
            raise KeyboardInterrupt
        return await original(launch, **kw)

    labeller.label_one = stop_after_one  # type: ignore[assignment]
    stats = _run(labeller.run(use_ohlcv=False))

    assert stats.labelled == 1, "whatever finished before the interrupt is committed"
    assert db.counts()["outcomes"] == 1
    runs = [r for r in db.recent_runs(limit=20) if r["surface"] == LABEL_SURFACE]
    assert len(runs) == 1
    assert runs[0]["ok"] is False
    assert "interrupted" in (runs[0]["error"] or "")
    db.close()


def test_two_passes_in_the_same_second_leave_two_rows(tmp_path):
    """`record_run` replaces on `run_id`, so a per-second id would lose one.

    Observed live: two passes a fraction of a second apart both wrote
    `label-1785472603`.
    """
    db = Database(str(tmp_path / "runid.db"))
    for _ in range(2):
        _run(OutcomeLabeller(Settings(), db=db, policy=LabelPolicy()).run(use_ohlcv=False))

    runs = [r for r in db.recent_runs(limit=20) if r["surface"] == LABEL_SURFACE]
    assert len({r["run_id"] for r in runs}) == 2
    db.close()


def test_a_launch_with_no_snapshots_is_counted_not_labelled(tmp_path):
    """Nothing to reconstruct a path from is a reported gap, not a zero outcome."""
    from botsensai.models import utcnow

    db = Database(str(tmp_path / "bare.db"))
    created = utcnow() - timedelta(hours=48)
    db.upsert_launch(
        Launch(token=_token("BAREMINT"), created_at=created, observed_at=created, source="pumpfun")
    )

    stats = _run(OutcomeLabeller(Settings(), db=db, policy=LabelPolicy()).run(use_ohlcv=False))

    assert stats.considered == 1
    assert stats.labelled == 0
    assert stats.skipped_no_path == 1
    assert db.counts()["outcomes"] == 0
    db.close()


def test_a_dead_ohlcv_pool_costs_one_token_not_the_pass(tmp_path):
    """A collector that raises must not stop the labelling run."""
    db = Database(str(tmp_path / "dead.db"))
    from botsensai.models import utcnow

    created = utcnow() - timedelta(hours=48)
    launch = Launch(
        token=_token("POOLMINT"), created_at=created, observed_at=created, source="pumpfun"
    )
    db.upsert_launch(launch)
    stamp = created + timedelta(seconds=60)
    db.insert_snapshots(
        [
            MarketSnapshot(
                token=launch.token,
                as_of=stamp,
                observed_at=stamp,
                price_usd=1e-6,
                liquidity_usd=5_000.0,
                pair_address="POOLX",
                source="dexscreener",
            )
        ]
    )

    class ExplodingGecko:
        async def ohlcv(self, *a, **kw):
            raise RuntimeError("pool not found")

    labeller = OutcomeLabeller(Settings(), db=db, policy=LabelPolicy(), gecko=ExplodingGecko())
    stats = _run(labeller.run(use_ohlcv=True))

    assert stats.labelled == 1, "the stored snapshot still yields a label"
    assert any("pool not found" in e for e in stats.errors)
    runs = [r for r in db.recent_runs(limit=20) if r["surface"] == LABEL_SURFACE]
    assert runs[0]["ok"] is False
    db.close()


def test_the_ohlcv_budget_is_a_hard_cap_on_calls(tmp_path):
    """GeckoTerminal shares 30 calls a minute with every other collector.

    Two calls per token means an unbounded pass would starve the sweep loop for
    the better part of an hour, so the budget stops the fetching without
    stopping the labelling.

    The budget counts attempts. Counting successes instead is the mistake the
    websocket loop made with `max_connections`, and it fails identically here:
    most of these pools are dead and return nothing, so every miss would be
    free and the real spend unbounded — measured live, 8 tokens attempted and
    only 3 returned candles.
    """
    db = Database(str(tmp_path / "budget.db"))
    from botsensai.models import utcnow

    for index in range(4):
        created = utcnow() - timedelta(hours=48, minutes=index)
        launch = Launch(
            token=_token(f"BUDGET{index}"),
            created_at=created,
            observed_at=created,
            source="pumpfun",
        )
        db.upsert_launch(launch)
        stamp = created + timedelta(seconds=60)
        db.insert_snapshots(
            [
                MarketSnapshot(
                    token=launch.token,
                    as_of=stamp,
                    observed_at=stamp,
                    price_usd=1e-6,
                    liquidity_usd=5_000.0,
                    pair_address=f"POOL{index}",
                    source="dexscreener",
                )
            ]
        )

    class DeadPoolGecko:
        """Every pool is dead — the case that makes a success-counted budget leak."""

        def __init__(self) -> None:
            self.calls = 0

        async def ohlcv(self, pool, **kw):
            self.calls += 1
            return []

    gecko = DeadPoolGecko()
    labeller = OutcomeLabeller(Settings(), db=db, policy=LabelPolicy(), gecko=gecko)
    stats = _run(labeller.run(use_ohlcv=True, max_ohlcv=2))

    assert stats.labelled == 4, "the budget bounds fetching, not labelling"
    assert stats.ohlcv_attempts == 2
    assert stats.ohlcv_fetched == 0
    assert gecko.calls == 4, (
        "two endpoints for each of the two tokens inside the budget, and nothing "
        "for the other two — a budget counting successes would have called eight times"
    )
    db.close()


def test_outcome_spread_separates_the_two_multiples(tmp_path):
    """The store-level read the CLI prints, and the number worth watching."""
    from botsensai.models import Outcome, utcnow

    db = Database(str(tmp_path / "spread.db"))
    for index, (peak, realizable) in enumerate(((10.0, 2.0), (4.0, 1.5), (40.0, 3.0))):
        db.upsert_outcome(
            Outcome(
                token=_token(f"SPREAD{index}"),
                labeled_at=utcnow(),
                max_multiple_from_t0=peak,
                max_realizable_multiple=realizable,
            )
        )
    db.upsert_outcome(Outcome(token=_token("NOT0"), labeled_at=utcnow()))

    spread = db.outcome_spread()

    assert spread["labelled"] == 4
    assert spread["with_both"] == 3
    assert spread["peak"]["n"] == 3, "the unknown-t0 outcome must not enter as a zero"
    assert spread["peak"]["median"] == 10.0
    assert spread["realizable"]["median"] == 2.0
    assert spread["realizable"]["max"] == 3.0
    db.close()
