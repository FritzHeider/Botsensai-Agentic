"""Wallet prior-history index: time restriction, batching and the cache.

`MetricContext.wallet_priors` feeds `fresh_wallet_ratio` and
`sniper_supply_share`, and both fail quietly when it is wrong rather than
raising. An empty index makes manufactured demand read as organic; an index that
counts trades nobody had observed yet makes a backtest look better than the live
system can be. Neither shows up as a failure anywhere else, so it shows up here.
"""

from __future__ import annotations

from datetime import timedelta

from botsensai.metrics.base import MetricContext
from botsensai.metrics.topology import FreshWalletRatio
from botsensai.models import Side, TokenRef, Trade, utcnow
from botsensai.onchain.wallet_priors import WalletPriorIndex
from botsensai.store.db import Database
from botsensai.util.synthetic import generate_token

TOKEN = TokenRef(mint="PriorsTestMint1111111111111111111111111111")


def _trade(
    wallet: str,
    as_of,
    observed_at=None,
    *,
    token: TokenRef | None = None,
    sig: str | None = None,
    side: Side = Side.BUY,
) -> Trade:
    ref = token or TOKEN
    return Trade(
        token=ref,
        signature=sig or f"{wallet}-{as_of.timestamp()}-{ref.mint[:6]}",
        as_of=as_of,
        observed_at=observed_at if observed_at is not None else as_of,
        wallet=wallet,
        side=side,
        amount_token=1000.0,
        amount_native=0.5,
        price_usd=0.001,
        source="test",
    )


# --------------------------------------------------------------------------- #
# time restriction — the point of the whole index
# --------------------------------------------------------------------------- #


def test_prior_count_is_restricted_to_trades_before_the_cutoff():
    """A wallet's history is what it did *before* the token existed, not in total."""
    db = Database(":memory:")
    launch_at = utcnow() - timedelta(hours=2)

    # Three trades before the cutoff, four after it.
    db.insert_trades(
        [_trade("WALLET_A", launch_at - timedelta(minutes=m), sig=f"before-{m}") for m in (90, 60, 30)]
        + [_trade("WALLET_A", launch_at + timedelta(minutes=m), sig=f"after-{m}") for m in (1, 5, 9, 20)]
    )

    index = WalletPriorIndex(db)
    assert index.prior_for("WALLET_A", launch_at) == 3, "counted trades from after the cutoff"

    # And the bound is strict: a trade exactly at the cutoff is not prior to it.
    db.insert_trades([_trade("WALLET_B", launch_at, sig="exactly-at")])
    assert index.prior_for("WALLET_B", launch_at) == 0


def test_prior_count_excludes_trades_not_yet_observed():
    """The subtle half: a trade that happened before, but that we learned of after.

    Measured on the real store when this index was built, 21% of the prior trades
    the backtester credited wallets with fell in this gap.
    """
    db = Database(":memory:")
    launch_at = utcnow() - timedelta(hours=2)

    known = _trade(
        "WALLET_C",
        launch_at - timedelta(minutes=30),
        launch_at - timedelta(minutes=29),
        sig="known-in-time",
    )
    # Same event time, but backfilled hours after the decision instant.
    backfilled = _trade(
        "WALLET_C",
        launch_at - timedelta(minutes=30),
        launch_at + timedelta(hours=6),
        sig="backfilled-later",
    )
    db.insert_trades([known, backfilled])

    index = WalletPriorIndex(db)

    both_bounds = index.prior_for("WALLET_C", launch_at, observed_before=launch_at)
    assert both_bounds == 1, "a trade observed after the decision leaked into its priors"

    # Without the knowledge bound both rows count — which is exactly the bug.
    assert index.prior_for("WALLET_C", launch_at) == 2

    # And once the backfill has been observed, it legitimately counts.
    later = launch_at + timedelta(hours=7)
    assert index.prior_for("WALLET_C", launch_at, observed_before=later) == 2


def test_database_prior_counts_answer_for_every_wallet_asked():
    """A wallet with no history maps to 0, not to a missing key.

    `fresh_wallet_ratio` reads a missing key as "unknown history" and drops the
    wallet from its numerator while keeping it in the denominator. Silently
    returning a short dict would bias the metric bullish.
    """
    db = Database(":memory:")
    cutoff = utcnow() - timedelta(hours=1)
    db.insert_trades([_trade("HAS_HISTORY", cutoff - timedelta(minutes=10))])

    counts = db.wallet_prior_counts(["HAS_HISTORY", "NEVER_TRADED"], cutoff)
    assert counts == {"HAS_HISTORY": 1, "NEVER_TRADED": 0}

    index = WalletPriorIndex(db)
    priors = index.priors_for(["HAS_HISTORY", "NEVER_TRADED"], cutoff)
    assert set(priors) == {"HAS_HISTORY", "NEVER_TRADED"}
    assert priors["NEVER_TRADED"] == 0


def test_prior_counts_span_tokens():
    """History means history everywhere, not on the token being scored."""
    db = Database(":memory:")
    other = TokenRef(mint="OtherMint222222222222222222222222222222222")
    cutoff = utcnow() - timedelta(hours=1)
    db.insert_trades(
        [
            _trade("WALLET_D", cutoff - timedelta(minutes=40), token=other, sig="other-1"),
            _trade("WALLET_D", cutoff - timedelta(minutes=20), token=other, sig="other-2"),
        ]
    )
    assert WalletPriorIndex(db).prior_for("WALLET_D", cutoff) == 2


# --------------------------------------------------------------------------- #
# cache correctness
# --------------------------------------------------------------------------- #


def test_cache_key_includes_the_time_bounds():
    """Keying the LRU on the wallet alone would serve one token's answer to another."""
    db = Database(":memory:")
    base = utcnow() - timedelta(hours=5)
    db.insert_trades(
        [_trade("WALLET_E", base + timedelta(minutes=m), sig=f"e-{m}") for m in (10, 20, 30, 40)]
    )

    index = WalletPriorIndex(db)
    early = index.prior_for("WALLET_E", base + timedelta(minutes=25))
    late = index.prior_for("WALLET_E", base + timedelta(hours=1))
    assert early == 2
    assert late == 4, "the second cutoff returned the first cutoff's cached answer"


def test_repeated_lookups_hit_the_cache_instead_of_the_database():
    db = Database(":memory:")
    cutoff = utcnow() - timedelta(hours=1)
    db.insert_trades([_trade("WALLET_F", cutoff - timedelta(minutes=5))])

    index = WalletPriorIndex(db)
    first = index.priors_for(["WALLET_F"], cutoff)
    queried_after_first = index.queried
    for _ in range(20):
        assert index.priors_for(["WALLET_F"], cutoff) == first

    assert index.queried == queried_after_first, "the cache did not prevent a re-query"
    assert index.hits >= 20


def test_cache_evicts_at_capacity_and_stays_correct():
    db = Database(":memory:")
    cutoff = utcnow() - timedelta(hours=1)
    db.insert_trades(
        [_trade(f"W{i}", cutoff - timedelta(minutes=5), sig=f"cap-{i}") for i in range(10)]
    )

    index = WalletPriorIndex(db, capacity=3)
    wallets = [f"W{i}" for i in range(10)]
    first = index.priors_for(wallets, cutoff)
    assert index.stats()["cached"] == 3, "capacity was not enforced"
    # Evicted entries must be recomputed to the same values, not lost.
    assert index.priors_for(wallets, cutoff) == first
    assert all(v == 1 for v in first.values())


def test_knowledge_bound_is_floored_never_raised():
    """Bucketing the knowledge bound is only safe in one direction.

    Rounding down can drop a trade we had just observed, which understates
    priors and makes the wallet read fresher. Rounding *up* would admit a trade
    observed after the decision instant, which is the leak this index exists to
    close. So the bucket must never move the bound forward.
    """
    db = Database(":memory:")
    launch_at = utcnow() - timedelta(hours=2)
    decision = launch_at + timedelta(seconds=30)

    # Observed 5 seconds before the decision, but inside the same 60s bucket
    # that the decision floors to — so the conservative answer excludes it.
    db.insert_trades(
        [
            _trade(
                "WALLET_H",
                launch_at - timedelta(minutes=10),
                decision - timedelta(seconds=5),
                sig="just-observed",
            )
        ]
    )

    index = WalletPriorIndex(db, observed_bucket_seconds=60.0)
    exact = WalletPriorIndex(db, observed_bucket_seconds=0.0)

    bucketed_count = index.prior_for("WALLET_H", launch_at, observed_before=decision)
    exact_count = exact.prior_for("WALLET_H", launch_at, observed_before=decision)

    assert exact_count == 1
    assert bucketed_count <= exact_count, "the bucket admitted data the exact bound excluded"


def test_bucketing_lets_the_cache_serve_wallets_across_tokens():
    """Without flooring, every token's own `utcnow()` is a distinct key and the LRU is dead."""
    db = Database(":memory:")
    launch_at = utcnow() - timedelta(hours=3)
    db.insert_trades([_trade("SHARED_BOT", launch_at - timedelta(hours=1), sig="bot-1")])

    index = WalletPriorIndex(db, observed_bucket_seconds=60.0)
    # Anchor mid-bucket so the offsets below cannot straddle a boundary; a
    # wall-clock `utcnow()` here would make this test flake once a minute.
    now = utcnow()
    anchor = now - timedelta(seconds=now.timestamp() % 60.0) + timedelta(seconds=30)
    # Several tokens scored a few seconds apart, as a real sweep does.
    for offset in (0.0, 3.5, 11.25):
        index.priors_for(
            ["SHARED_BOT"], launch_at, observed_before=anchor + timedelta(seconds=offset)
        )

    assert index.queried == 1, f"the shared wallet was re-queried {index.queried} times"
    assert index.hits >= 2


# --------------------------------------------------------------------------- #
# the wallet_profiles maintainer
# --------------------------------------------------------------------------- #


def test_refresh_wallet_profiles_summarises_trades():
    db = Database(":memory:")
    base = utcnow() - timedelta(hours=3)
    db.insert_trades(
        [_trade("WALLET_G", base + timedelta(minutes=m), sig=f"g-{m}") for m in (0, 30, 90)]
    )

    written = db.refresh_wallet_profiles()
    assert written == 1

    row = db.conn.execute(
        "SELECT * FROM wallet_profiles WHERE wallet = 'WALLET_G'"
    ).fetchone()
    assert row["trade_count"] == 3
    assert abs(row["first_seen_at"] - base.timestamp()) < 1.0
    assert abs(row["last_seen_at"] - (base + timedelta(minutes=90)).timestamp()) < 1.0


def test_profile_fast_path_does_not_change_the_answer():
    """The `first_seen_at` shortcut may skip a query; it may not skip the truth."""
    db = Database(":memory:")
    cutoff = utcnow() - timedelta(hours=1)
    # BORN_LATER has trades, but all of them after the cutoff.
    db.insert_trades(
        [_trade("BORN_LATER", cutoff + timedelta(minutes=m), sig=f"late-{m}") for m in (5, 10)]
        + [_trade("BORN_EARLIER", cutoff - timedelta(minutes=5), sig="early-1")]
    )
    db.refresh_wallet_profiles()

    index = WalletPriorIndex(db)
    priors = index.priors_for(["BORN_LATER", "BORN_EARLIER"], cutoff)
    assert priors == {"BORN_LATER": 0, "BORN_EARLIER": 1}
    assert index.skipped_by_profile == 1, "the profile fast path never fired"

    # Same answers with no profile rows at all, i.e. the shortcut is an
    # optimisation and not a source of truth.
    bare = Database(":memory:")
    bare.insert_trades(
        [_trade("BORN_LATER", cutoff + timedelta(minutes=m), sig=f"late-{m}") for m in (5, 10)]
        + [_trade("BORN_EARLIER", cutoff - timedelta(minutes=5), sig="early-1")]
    )
    assert WalletPriorIndex(bare).priors_for(["BORN_LATER", "BORN_EARLIER"], cutoff) == priors


def test_refresh_scoped_to_wallets_leaves_others_alone():
    db = Database(":memory:")
    base = utcnow() - timedelta(hours=2)
    db.insert_trades(
        [
            _trade("SCOPED_IN", base, sig="in-1"),
            _trade("SCOPED_OUT", base, sig="out-1"),
        ]
    )
    assert db.refresh_wallet_profiles(["SCOPED_IN"]) == 1
    present = {
        r["wallet"] for r in db.conn.execute("SELECT wallet FROM wallet_profiles").fetchall()
    }
    assert present == {"SCOPED_IN"}
    assert db.refresh_wallet_profiles([]) == 0


# --------------------------------------------------------------------------- #
# the reason any of this matters: the metric moves
# --------------------------------------------------------------------------- #


def test_fresh_wallet_ratio_separates_fresh_from_established_buyers():
    """With priors present the metric must distinguish the two populations.

    Without them every buyer falls through to the holder-age fallback, and with
    no holder records that means every buyer is skipped — the metric reports a
    number built from an empty numerator over a full denominator.
    """
    db = Database(":memory:")
    launch_at = utcnow() - timedelta(minutes=30)
    metric = FreshWalletRatio()

    fresh_buyers = [f"FRESH_{i}" for i in range(12)]
    aged_buyers = [f"AGED_{i}" for i in range(12)]

    # Aged buyers have a real history before the launch; fresh ones have none.
    history = []
    for w in aged_buyers:
        history += [
            _trade(w, launch_at - timedelta(hours=h), sig=f"hist-{w}-{h}") for h in range(1, 26)
        ]
    db.insert_trades(history)

    def ratio(buyers: list[str]) -> float:
        trades = [
            _trade(w, launch_at + timedelta(minutes=1), sig=f"buy-{w}") for w in buyers
        ]
        priors = WalletPriorIndex(db).priors_for(buyers, launch_at)
        ctx = MetricContext(
            token=TOKEN, as_of=launch_at + timedelta(minutes=5), trades=trades, wallet_priors=priors
        )
        value, evidence, _ = metric.compute(ctx)
        assert value is not None and evidence == len(buyers)
        return value

    fresh_ratio = ratio(fresh_buyers)
    aged_ratio = ratio(aged_buyers)

    assert fresh_ratio > 0.9, f"a buy side of brand-new wallets read as {fresh_ratio:.2f}"
    assert aged_ratio < 0.3, f"a buy side of established wallets read as {aged_ratio:.2f}"


def test_live_context_populates_wallet_priors():
    """The regression this task exists for: the live path passed a literal `{}`."""
    from botsensai.pipeline import Pipeline

    db = Database(":memory:")
    token = generate_token("organic", seed=11)
    db.upsert_launch(token.launch)
    db.insert_trades(token.trades)
    # Give one of this token's buyers a history predating the launch.
    buyer = next(t.wallet for t in token.trades if t.side is Side.BUY)
    db.insert_trades(
        [
            _trade(
                buyer,
                token.launch.created_at - timedelta(hours=h),
                token=token.launch.token,
                sig=f"prior-{h}",
            )
            for h in range(1, 6)
        ]
    )

    pipeline = Pipeline(db=db, collectors=[])
    ctx = pipeline.build_context(token.launch, as_of=token.launch.created_at + timedelta(minutes=10))

    assert ctx.wallet_priors, "wallet_priors is still empty in the live path"
    assert ctx.wallet_priors.get(buyer) == 5
    assert set(ctx.wallet_priors) >= {t.wallet for t in ctx.trades}


def test_backtest_tapes_bound_priors_on_both_axes():
    """Tape priors must not credit a wallet with history observed after the launch."""
    from botsensai.backtest.engine import Backtester

    db = Database(":memory:")
    token = generate_token("organic", seed=23)
    db.upsert_launch(token.launch)
    db.insert_trades(token.trades)

    buyer = next(t.wallet for t in token.trades if t.side is Side.BUY)
    launch_at = token.launch.created_at
    db.insert_trades(
        [
            _trade(buyer, launch_at - timedelta(hours=2), launch_at - timedelta(hours=2), sig="seen"),
            _trade(buyer, launch_at - timedelta(hours=2), launch_at + timedelta(days=1), sig="late"),
        ]
    )

    tapes = Backtester.tapes_from_database(
        db, launch_at - timedelta(hours=1), launch_at + timedelta(hours=6)
    )
    tape = next(t for t in tapes if t.launch.token.key == token.launch.token.key)
    assert tape.wallet_priors[buyer] == 1, "a backfilled trade leaked into the tape's priors"
