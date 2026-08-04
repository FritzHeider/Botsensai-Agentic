"""Integrity tests: point-in-time correctness, memory, store and content safety.

The tests in this file exist because each of them corresponds to a failure that
would not announce itself. A look-ahead leak produces a *better* backtest, a
memory leak produces a *more confident* agent, and a missing disclosure produces
a post that reads fine. None of these fail loudly in production, so they have to
fail loudly here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from botsensai.backtest.engine import Backtester
from botsensai.media import FORBIDDEN_PATTERNS, ContentGenerator
from botsensai.memory.store import MemoryStore
from botsensai.metrics import build_registry
from botsensai.models import (
    MemoryKind,
    Outcome,
    utcnow,
)
from botsensai.scoring import CompositeScorer
from botsensai.scoring.fit import (
    MIN_SAMPLES_TO_FIT,
    TrainingExample,
    WeightFitter,
    spearman,
    top_decile_lift,
)
from botsensai.store.db import Database
from botsensai.util.synthetic import generate_cohort, generate_token
from tests.test_metrics import context_for

# --------------------------------------------------------------------------- #
# point-in-time correctness
# --------------------------------------------------------------------------- #


def test_tape_context_never_includes_future_records():
    token = generate_token("organic", seed=17)
    tape = Backtester.tapes_from_synthetic([token])[0]
    midpoint = tape.launch.created_at + timedelta(seconds=1200)
    ctx = tape.context_at(midpoint)

    assert all(s.as_of <= midpoint for s in ctx.snapshots)
    assert all(t.as_of <= midpoint for t in ctx.trades)
    assert all(p.as_of <= midpoint for p in ctx.posts)
    assert all(h.as_of <= midpoint for h in ctx.holders)
    assert len(ctx.trades) < len(tape.trades), "the window must actually exclude something"


def test_holders_collapse_to_a_single_slice():
    """Holders are stored as repeated snapshots; a metric must see one picture.

    Handing a metric every historical balance for a wallet would make holder
    counts and concentration figures nonsense in a way that is easy to miss.
    """
    token = generate_token("organic", seed=17)
    tape = Backtester.tapes_from_synthetic([token])[0]
    ctx = tape.context_at(tape.launch.created_at + timedelta(seconds=1800))
    wallets = [h.wallet for h in ctx.holders]
    assert len(wallets) == len(set(wallets)), "a wallet appeared more than once in one context"
    if ctx.holders:
        assert len({h.as_of for h in ctx.holders}) == 1


def test_database_as_of_reads_respect_observed_at():
    """Data backfilled later must be invisible to a decision made earlier.

    This is the subtlest look-ahead vector in the system: a record whose `as_of`
    is in the past but which we did not actually learn until much later.
    """
    db = Database(":memory:")
    token = generate_token("organic", seed=5)
    db.upsert_launch(token.launch)

    decision_time = token.launch.created_at + timedelta(seconds=600)

    backfilled = token.snapshots[1].model_copy(
        update={
            "as_of": token.launch.created_at + timedelta(seconds=300),
            "observed_at": decision_time + timedelta(hours=2),
            "source": "backfill",
        }
    )
    live = token.snapshots[1].model_copy(
        update={
            "as_of": token.launch.created_at + timedelta(seconds=300),
            "observed_at": token.launch.created_at + timedelta(seconds=305),
            "source": "live",
        }
    )
    db.insert_snapshots([backfilled, live])

    visible = db.snapshots_as_of(token.token.key, decision_time)
    sources = {s.source for s in visible}
    assert "live" in sources
    assert "backfill" not in sources, "a record observed after the decision leaked into it"


def test_account_snapshots_respect_observed_at():
    """A profile read after the decision must be invisible to it.

    Profiles are the one record type where the two timestamps collapse: what an
    account's follower count *was* is only knowable by having looked, so the
    observation time is the only bound there is. That makes it the only thing
    standing between a backtest and a promoter profile from next week.
    """
    from botsensai.models import Platform, SocialAccount

    db = Database(":memory:")
    token = generate_token("organic", seed=5)
    db.upsert_launch(token.launch)
    decision_time = token.launch.created_at + timedelta(seconds=600)

    base = {
        "platform": Platform.X,
        "handle": "TokenTeam",
        "token_key": token.token.key,
        "role": "promoter",
        "created_at": token.launch.created_at - timedelta(days=900),
    }
    db.insert_accounts(
        [
            SocialAccount(**base, followers=100, observed_at=decision_time - timedelta(minutes=5)),
            SocialAccount(**base, followers=50_000, observed_at=decision_time + timedelta(hours=2)),
        ]
    )

    visible = db.accounts_as_of(token.token.key, decision_time)
    assert [a.followers for a in visible] == [100], (
        "the later reading of the same profile leaked into an earlier decision"
    )
    # And the later reading is not lost, merely not yet visible.
    assert [a.followers for a in db.accounts_as_of(token.token.key, decision_time + timedelta(days=1))] == [
        50_000
    ]


def test_account_handles_are_stored_case_folded():
    """`@TokenTeam` and `@tokenteam` are one account. Two rows would rank as two
    promoters, and `MetricContext.promoter` would pick between them by clock."""
    from botsensai.models import Platform, SocialAccount

    db = Database(":memory:")
    token = generate_token("organic", seed=5)
    db.upsert_launch(token.launch)
    when = token.launch.created_at + timedelta(seconds=60)

    db.insert_accounts(
        [
            SocialAccount(
                platform=Platform.X, handle=spelling, token_key=token.token.key,
                role="promoter", followers=n, observed_at=when,
            )
            for spelling, n in (("TokenTeam", 1), ("tokenteam", 2))
        ]
    )
    stored = db.accounts_as_of(token.token.key, when + timedelta(seconds=1))
    assert len(stored) == 1, f"one account stored under two spellings: {stored}"
    assert stored[0].handle == "tokenteam"


def test_untagged_accounts_are_refused_rather_than_written():
    """`accounts_as_of` filters on `token_key`, so an untagged row is stored
    complete and unreadable — and because SQLite treats NULLs in a primary key
    as distinct, it does not even deduplicate against itself."""
    from botsensai.models import Platform, SocialAccount

    db = Database(":memory:")
    account = SocialAccount(platform=Platform.X, handle="drifter", followers=10)
    assert db.insert_accounts([account, account]) == 0
    assert db.conn.execute("SELECT COUNT(*) FROM social_accounts").fetchone()[0] == 0


def test_account_snapshots_do_not_cross_tokens():
    """One handle promoting two launches is two rows, not one overwritten row."""
    from botsensai.models import Platform, SocialAccount

    db = Database(":memory:")
    first = generate_token("organic", seed=5)
    second = generate_token("organic", seed=6)
    when = first.launch.created_at + timedelta(seconds=60)
    db.insert_accounts(
        [
            SocialAccount(
                platform=Platform.X, handle="serialpromoter", token_key=t.token.key,
                role="promoter", followers=n, observed_at=when,
            )
            for t, n in ((first, 11), (second, 22))
        ]
    )
    read = when + timedelta(seconds=1)
    assert [a.followers for a in db.accounts_as_of(first.token.key, read)] == [11]
    assert [a.followers for a in db.accounts_as_of(second.token.key, read)] == [22]


def test_deployer_history_is_time_restricted():
    """Scoring a deployer on outcomes that had not happened yet is the classic leak."""
    db = Database(":memory:")
    early = generate_token("rug", seed=1)
    later = generate_token("organic", seed=2)
    # Force a shared deployer and a clear time ordering.
    early.launch.deployer = "DEPLOYER"
    later.launch.deployer = "DEPLOYER"
    later.launch.created_at = early.launch.created_at + timedelta(hours=4)

    db.upsert_launch(early.launch)
    db.upsert_launch(later.launch)
    db.upsert_outcome(
        Outcome(token=early.token, rugged=True, max_multiple_from_t0=0.1, labeled_at=utcnow())
    )

    before_first = db.deployer_history("DEPLOYER", before=early.launch.created_at)
    assert before_first["launch_count"] == 0

    at_second = db.deployer_history("DEPLOYER", before=later.launch.created_at)
    assert at_second["launch_count"] == 1
    assert at_second["rug_count"] == 1


def test_backtest_runs_end_to_end_and_labels_synthetic_output():
    cohort = generate_cohort(n=25, seed=404)
    tapes = Backtester.tapes_from_synthetic(cohort)
    result = Backtester().run(tapes, synthetic=True, starting_native=10.0)
    summary = result.summary()

    assert result.evaluated > 0
    assert summary["synthetic"] is True
    assert "warning_synthetic" in summary, "a synthetic run must say so in its own summary"
    assert result.universe_size == 25


def test_backtest_reports_thin_samples_honestly():
    cohort = generate_cohort(n=12, seed=77)
    result = Backtester().run(Backtester.tapes_from_synthetic(cohort), synthetic=True)
    if len(result.trades) < 30:
        assert "warning" in result.summary()


def test_backtest_is_deterministic():
    cohort = generate_cohort(n=20, seed=808)
    tapes = Backtester.tapes_from_synthetic(cohort)
    first = Backtester().run(tapes, synthetic=True)
    second = Backtester().run(tapes, synthetic=True)
    assert first.entered == second.entered
    assert pytest.approx(first.total_pnl_native) == second.total_pnl_native


# --------------------------------------------------------------------------- #
# agentic memory
# --------------------------------------------------------------------------- #


def test_memory_recall_is_time_bounded():
    """Memory is the easiest place to leak the future into a backtest."""
    store = MemoryStore(":memory:")
    t0 = utcnow()
    store.remember(
        MemoryKind.HEURISTIC, "global", "early", "known at t0", confidence=0.9, created_at=t0
    )
    store.remember(
        MemoryKind.HEURISTIC,
        "global",
        "late",
        "not known until later",
        confidence=0.9,
        created_at=t0 + timedelta(hours=3),
    )

    early_view = [e.title for e in store.recall(as_of=t0 + timedelta(minutes=10))]
    assert early_view == ["early"]

    later_view = {e.title for e in store.recall(as_of=t0 + timedelta(hours=4))}
    assert later_view == {"early", "late"}


def test_supersession_preserves_history():
    store = MemoryStore(":memory:")
    t0 = utcnow() - timedelta(hours=5)
    original = store.remember(
        MemoryKind.HEURISTIC, "global", "rule", "first version", confidence=0.5, created_at=t0
    )
    store.revise(original.id, "second version", confidence=0.8)

    past = store.recall(as_of=t0 + timedelta(minutes=1), apply_decay=False)
    assert past and past[0].body == "first version", "history must remain reconstructible"

    present = store.recall(as_of=utcnow(), apply_decay=False)
    assert any(e.body == "second version" for e in present)


def test_feedback_moves_confidence_in_the_right_direction():
    store = MemoryStore(":memory:")
    entry = store.remember(MemoryKind.HEURISTIC, "global", "rule", "body", confidence=0.5)
    up = store.record_feedback(entry.id, True)
    assert up > 0.5
    down = store.record_feedback(entry.id, False)
    assert down < up


def test_confidence_decays_with_age():
    store = MemoryStore(":memory:", decay_half_life_hours=1.0, min_confidence=0.3)
    old = utcnow() - timedelta(hours=6)
    store.remember(MemoryKind.HEURISTIC, "global", "stale", "body", confidence=0.6, created_at=old)
    assert store.recall(as_of=utcnow()) == [], "a stale low-confidence belief should decay out"
    assert store.recall(as_of=utcnow(), apply_decay=False), "but must remain retrievable"


def test_memory_briefing_is_prompt_ready():
    store = MemoryStore(":memory:")
    store.remember(
        MemoryKind.REGIME, "global", "Market regime: dead", "Graduation rate 0.2%", confidence=0.8
    )
    briefing = store.briefing()
    assert "regime" in briefing
    assert "Graduation rate" in briefing


# --------------------------------------------------------------------------- #
# weight fitting
# --------------------------------------------------------------------------- #


def test_fitter_refuses_to_fit_on_a_small_sample():
    report = WeightFitter().fit([])
    assert not report.fitted
    assert str(MIN_SAMPLES_TO_FIT) in report.warnings[0]
    assert report.weights.version == "v0-default"


def test_rank_correlation_helpers():
    assert spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    scores = list(range(100))
    labels = list(range(100))
    assert top_decile_lift(scores, labels) > 1.5


def test_training_example_targets_realizable_not_peak():
    """Training on unreachable peaks teaches the model a useless skill."""
    token = generate_token("organic", seed=3)
    outcome = Outcome(
        token=token.token,
        max_multiple_from_t0=100.0,
        max_realizable_multiple=2.0,
        labeled_at=utcnow(),
    )
    realizable = TrainingExample.from_values("k", utcnow(), [], outcome, target="realizable")
    peak = TrainingExample.from_values("k", utcnow(), [], outcome, target="peak")
    assert realizable.label < peak.label


def test_rugged_outcomes_are_capped_regardless_of_peak():
    token = generate_token("rug", seed=3)
    outcome = Outcome(
        token=token.token, rugged=True, max_multiple_from_t0=50.0, labeled_at=utcnow()
    )
    example = TrainingExample.from_values("k", utcnow(), [], outcome)
    assert example.label < 0.1, "a rug that spiked first is still a rug"


# --------------------------------------------------------------------------- #
# content safety
# --------------------------------------------------------------------------- #


def test_generated_posts_always_carry_disclosure():
    token = generate_token("organic", seed=3)
    scorer = CompositeScorer(build_registry())
    score = scorer.score(context_for("organic", seed=3))
    generator = ContentGenerator()

    for piece in (
        generator.blog_post(token.launch, score),
        generator.thread(token.launch, score),
        generator.alert(token.launch, score, "watch"),
    ):
        assert generator.media.disclosure_text in piece.body or piece.disclosure in piece.body


def test_holding_a_position_forces_explicit_disclosure():
    token = generate_token("organic", seed=3)
    score = CompositeScorer(build_registry()).score(context_for("organic", seed=3))
    piece = ContentGenerator().blog_post(token.launch, score, holds_position=True)
    assert "Position disclosure" in piece.body


def test_forbidden_phrasing_is_rejected_after_rendering():
    """The guardrail must run on output, not trust the templates."""
    token = generate_token("organic", seed=3)
    score = CompositeScorer(build_registry()).score(context_for("organic", seed=3))
    generator = ContentGenerator()
    piece = generator.blog_post(token.launch, score)

    piece.body += "\n\nThis will 100x, guaranteed."
    with pytest.raises(ValueError, match="prohibited phrasing"):
        generator._enforce(piece)


def test_forbidden_patterns_cover_the_obvious_claims():
    samples = [
        "this will moon",
        "guaranteed returns",
        "risk-free entry",
        "you can't lose",
        "a sure thing",
    ]
    for sample in samples:
        assert any(p.search(sample) for p in FORBIDDEN_PATTERNS), sample


def test_disclaimer_language_is_not_itself_flagged():
    """'Not financial advice' must pass; 'this is financial advice' must not."""
    assert not any(p.search("Not financial advice.") for p in FORBIDDEN_PATTERNS)
    assert any(p.search("Treat this as financial advice.") for p in FORBIDDEN_PATTERNS)


def test_every_claim_in_a_post_is_sourced():
    token = generate_token("organic", seed=3)
    score = CompositeScorer(build_registry()).score(context_for("organic", seed=3))
    piece = ContentGenerator().blog_post(token.launch, score)
    assert piece.facts_cited, "a generated post must carry its evidence trail"
    assert all("[" in fact and "]" in fact for fact in piece.facts_cited)


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_store_round_trips_every_record_type():
    db = Database(":memory:")
    token = generate_token("organic", seed=3)

    db.upsert_launch(token.launch)
    db.insert_snapshots(token.snapshots)
    db.insert_trades(token.trades)
    db.insert_holders(token.holders)
    db.insert_security(token.security)
    db.insert_posts(token.posts, token_key=token.token.key)

    counts = db.counts()
    assert counts["launches"] == 1
    assert counts["market_snapshots"] > 0
    assert counts["trades"] > 0
    assert counts["holders"] > 0
    assert counts["social_posts"] > 0

    loaded = db.launch(token.token.key)
    assert loaded is not None
    assert loaded.token.symbol == token.token.symbol
    assert loaded.deployer == token.launch.deployer


def test_store_dedupes_repeated_inserts():
    db = Database(":memory:")
    token = generate_token("organic", seed=3)
    db.insert_trades(token.trades)
    before = db.counts()["trades"]
    db.insert_trades(token.trades)
    assert db.counts()["trades"] == before


def test_scoring_persists_and_reloads():
    db = Database(":memory:")
    token = generate_token("organic", seed=3)
    db.upsert_launch(token.launch)
    score = CompositeScorer(build_registry()).score(context_for("organic", seed=3))
    db.insert_metric_values(score.metric_values)
    db.insert_score(score)

    reloaded = db.metric_values_as_of(token.token.key, utcnow())
    assert len(reloaded) > 0
    assert all(v.metric_id for v in reloaded)
