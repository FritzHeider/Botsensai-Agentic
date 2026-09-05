"""Metric tests.

The central assertion in this file is not that any metric returns a particular
number — those numbers will change as the metrics are refined. It is that the
metrics *separate the archetypes*: an organic launch must score higher than a
manufactured pump, which must score higher than a rug. A metric that fails to
separate them is not measuring anything, and a change that breaks the separation
is a regression regardless of how reasonable it looked.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from botsensai.media.phash import exact_label, perceptual_label
from botsensai.metrics import METRIC_CLASSES, build_registry
from botsensai.metrics.base import MetricContext
from botsensai.models import Confidence, Direction, TokenRef
from botsensai.util.synthetic import generate_token


def context_for(archetype: str, seed: int = 7, age_seconds: float = 3600.0) -> MetricContext:
    token = generate_token(archetype, seed=seed)
    as_of = token.launch.created_at + timedelta(seconds=age_seconds)
    latest_slice = max((h.as_of for h in token.holders), default=as_of)
    return MetricContext(
        token=token.token,
        as_of=as_of,
        launch=token.launch,
        snapshots=[s for s in token.snapshots if s.as_of <= as_of],
        trades=[t for t in token.trades if t.as_of <= as_of],
        holders=[h for h in token.holders if h.as_of == latest_slice],
        security=token.security,
        posts=[p for p in token.posts if p.as_of <= as_of],
        wallet_priors=token.wallet_priors,
        recent_narratives=[f"Unrelated Token {i} about something else entirely" for i in range(40)],
        extra={
            "market_regime": {
                "graduation_rate_24h": 0.008,
                "launches_per_hour": 250,
                "new_token_inflow_usd_1h": 400_000,
                "sample_size": 500,
            },
            "active_themes": [("frog animal meme", 0.8)],
            "target_position_usd": 40.0,
            "max_impact_pct": 0.05,
        },
    )


# --------------------------------------------------------------------------- #
# registry contract
# --------------------------------------------------------------------------- #


def test_registry_builds_and_is_complete():
    registry = build_registry()
    assert len(registry) == len(METRIC_CLASSES)
    assert len(registry) >= 20, "the brief called for at least 20 non-API metrics"


def test_every_metric_documents_its_gameability():
    """A metric with no stated counter-measure cannot be trusted in a composite."""
    for metric in build_registry():
        assert metric.gameability.strip(), f"{metric.id} has no gameability documentation"
        assert len(metric.gameability) > 80, f"{metric.id} gameability note is too thin to be useful"


def test_every_metric_has_a_thesis_and_sources():
    for metric in build_registry():
        assert metric.thesis.strip(), f"{metric.id} has no thesis"
        assert metric.family != "unassigned", f"{metric.id} has no family"
        assert metric.direction in Direction


def test_metric_ids_are_unique():
    ids = [cls.id for cls in METRIC_CLASSES]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# missing-data contract
# --------------------------------------------------------------------------- #


def test_empty_context_never_produces_a_confident_zero():
    """The most dangerous bug in this system would be absence reading as bearish.

    An empty context must yield MISSING with `normalized=None`, never 0.0 with
    high confidence — the latter is a strong bearish claim made from no data.
    """
    ctx = MetricContext(token=TokenRef(mint="1" * 32), as_of=generate_token("organic").launch.created_at)
    for metric in build_registry():
        value = metric.evaluate(ctx)
        if value.confidence is Confidence.MISSING:
            assert value.normalized is None, f"{metric.id} emitted a value while MISSING"
        else:
            assert value.confidence in (Confidence.LOW, Confidence.MEDIUM), (
                f"{metric.id} claimed {value.confidence.value} confidence on an empty context"
            )


def test_metrics_never_raise_on_partial_data():
    """A metric that raises must not be able to take down a scoring pass."""
    full = context_for("organic")
    partial = MetricContext(
        token=full.token,
        as_of=full.as_of,
        launch=full.launch,
        snapshots=full.snapshots[:1],
        trades=full.trades[:2],
        holders=[],
        posts=full.posts[:1],
    )
    for metric in build_registry():
        value = metric.evaluate(partial)
        assert value.metric_id == metric.id
        if value.normalized is not None:
            assert 0.0 <= value.normalized <= 1.0


def test_normalized_values_are_always_in_range():
    for archetype in ("organic", "manufactured", "rug"):
        ctx = context_for(archetype)
        for value in build_registry().evaluate_all(ctx):
            if value.normalized is not None:
                assert 0.0 <= value.normalized <= 1.0, f"{value.metric_id} out of range"


# --------------------------------------------------------------------------- #
# separation — the assertions that actually matter
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", [3, 11, 23])
def test_reply_template_ratio_catches_scripted_farms(seed):
    organic = context_for("organic", seed=seed)
    farm = context_for("manufactured", seed=seed)
    metric = next(m for m in build_registry() if m.id == "reply_template_ratio")

    organic_value = metric.evaluate(organic)
    farm_value = metric.evaluate(farm)

    assert organic_value.raw is not None and farm_value.raw is not None
    assert farm_value.raw > organic_value.raw + 0.3, (
        "scripted replies must show a much higher template-cluster share"
    )
    # And after inversion, organic must read as more bullish.
    assert (organic_value.normalized or 0) > (farm_value.normalized or 1)


@pytest.mark.parametrize("seed", [3, 11, 23])
def test_engager_age_dispersion_catches_batch_provisioned_fleets(seed):
    organic = context_for("organic", seed=seed)
    farm = context_for("manufactured", seed=seed)
    metric = next(m for m in build_registry() if m.id == "engager_age_dispersion")

    organic_value = metric.evaluate(organic)
    farm_value = metric.evaluate(farm)
    assert organic_value.raw is not None and farm_value.raw is not None
    assert organic_value.raw > farm_value.raw, (
        "a fleet registered in one batch must show lower account-age dispersion"
    )


@pytest.mark.parametrize("seed", [3, 11, 23])
def test_reply_rhythm_detects_metronomic_posting(seed):
    organic = context_for("organic", seed=seed)
    farm = context_for("manufactured", seed=seed)
    metric = next(m for m in build_registry() if m.id == "reply_rhythm_naturalness")

    organic_value = metric.evaluate(organic)
    farm_value = metric.evaluate(farm)
    assert organic_value.raw is not None and farm_value.raw is not None
    assert organic_value.raw > farm_value.raw, "scripted cadence must score lower than human bursts"


@pytest.mark.parametrize("seed", [3, 11, 23])
def test_bundle_supply_share_detects_same_slot_allocation(seed):
    organic = context_for("organic", seed=seed)
    rug = context_for("rug", seed=seed)
    metric = next(m for m in build_registry() if m.id == "bundle_supply_share")

    organic_value = metric.evaluate(organic)
    rug_value = metric.evaluate(rug)
    assert rug_value.raw is not None
    assert rug_value.raw > (organic_value.raw or 0.0), (
        "an atomic same-slot allocation must register as bundled supply"
    )


@pytest.mark.parametrize("seed", [3, 11, 23])
def test_funder_graph_collapses_sybil_clusters(seed):
    organic = context_for("organic", seed=seed)
    rug = context_for("rug", seed=seed)
    metric = next(m for m in build_registry() if m.id == "funder_graph_dispersion")

    organic_value = metric.evaluate(organic)
    rug_value = metric.evaluate(rug)
    assert organic_value.raw is not None and rug_value.raw is not None
    assert organic_value.raw > rug_value.raw, (
        "wallets funded from one distributor must collapse to fewer effective actors"
    )


def test_deployer_behaviour_detects_the_dump():
    rug = context_for("rug", seed=5)
    metric = next(m for m in build_registry() if m.id == "deployer_behaviour_now")
    value = metric.evaluate(rug)
    assert value.raw is not None
    assert value.raw < 0, "a deployer selling their bag must produce a negative raw reading"


def test_organic_media_production_only_fires_for_real_communities():
    organic = context_for("organic", seed=9)
    farm = context_for("manufactured", seed=9)
    metric = next(m for m in build_registry() if m.id == "organic_media_production_rate")

    organic_value = metric.evaluate(organic)
    farm_value = metric.evaluate(farm)
    assert (organic_value.raw or 0.0) > (farm_value.raw or 0.0), (
        "unpaid creative labour is the signal a manufactured launch cannot buy"
    )


def _remix_metric():
    return next(m for m in build_registry() if m.id == "derivative_remix_depth")


def _ctx_with_hashes(hashes: list[str]):
    """One post per hash, so the metric sees exactly the population given."""
    ctx = context_for("organic", age_seconds=3600.0)
    template = ctx.posts[0]
    ctx.posts = [
        template.model_copy(update={"post_id": f"m{i}", "media_hashes": [h]})
        for i, h in enumerate(hashes)
    ]
    return ctx


def test_remix_depth_ignores_exact_hashes():
    """An MD5 answers 'same file', which is not the question this metric asks.

    Eight distinct MD5s of the same picture re-encoded eight times would look
    like eight visual ideas. Absence is the honest reading, not a maximum.
    """
    md5s = [exact_label(f"{i:032x}") for i in range(8)]
    value = _remix_metric().evaluate(_ctx_with_hashes(md5s))
    assert value.raw is None
    assert value.confidence is Confidence.MISSING


def test_remix_depth_separates_reposting_from_remixing():
    """Reposting one image must not move the metric; drifting variants must.

    The reposts differ in their *top* bits, one bit each. Under Hamming they are
    one image; under any prefix bucketing they are eight distinct ones — which
    is the reading this metric used to give and the reason the clustering had to
    change. Flipping low bits instead would let prefix bucketing pass too.
    """
    metric = _remix_metric()
    base = 0x0F1E2D3C4B5A6978
    reposts = [perceptual_label(f"{base ^ (1 << (63 - i)):016x}") for i in range(8)]
    lineages = [
        perceptual_label(f"{(base ^ (0xFFFF << (16 * (i % 4)))) ^ (1 << i):016x}") for i in range(8)
    ]

    repost_value = metric.compute(_ctx_with_hashes(reposts))[0]
    remix_value = metric.compute(_ctx_with_hashes(lineages))[0]
    assert repost_value == 0.0, "one image posted eight times is not a remix population"
    assert remix_value is not None and remix_value > repost_value


# --------------------------------------------------------------------------- #
# temporal contract
# --------------------------------------------------------------------------- #


def test_metrics_respect_their_earliest_availability():
    """A metric evaluated before its window must not claim full confidence."""
    ctx = context_for("organic", age_seconds=30.0)
    for metric in build_registry():
        if metric.earliest_seconds <= 30.0:
            continue
        value = metric.evaluate(ctx)
        assert value.confidence in (Confidence.MISSING, Confidence.LOW), (
            f"{metric.id} claimed {value.confidence.value} at t+30s despite needing "
            f"t+{metric.earliest_seconds:.0f}s"
        )


def test_context_windows_never_look_forward():
    ctx = context_for("organic", age_seconds=1800.0)
    assert all(s.as_of <= ctx.as_of for s in ctx.snapshots)
    assert all(t.as_of <= ctx.as_of for t in ctx.trades)
    assert all(p.as_of <= ctx.as_of for p in ctx.posts)
    assert ctx.trades_within(300.0) == [
        t for t in ctx.trades if (ctx.as_of - t.as_of).total_seconds() <= 300.0
    ]


# --------------------------------------------------------------------------- #
# statistics primitives
# --------------------------------------------------------------------------- #


def test_unnormalized_entropy_is_not_clamped_to_one():
    """Regression: clamping nats flattened two metrics into near-constants.

    `shannon_entropy(..., normalize=False)` returns nats, which are unbounded
    above — n equal categories give ln(n). The clamp pinned every diverse
    distribution to exactly 1.0, so `mention_author_diversity`'s
    exp(h) "effective voices" was always e = 2.718: tokens with 7, 32 and 135
    distinct authors all reported 2.7 and normalized to ~0.175.
    `engager_age_dispersion` lost its cohort-count term the same way.
    """
    import math

    from botsensai.util.stats import shannon_entropy

    # 100 equally-active authors carry ln(100) nats, not 1.0.
    h = shannon_entropy([1.0] * 100, normalize=False)
    assert h > 4.0, f"nats were clamped: got {h}"
    assert abs(h - math.log(100)) < 1e-6

    # And the measure must still discriminate between crowd sizes.
    small = shannon_entropy([1.0] * 5, normalize=False)
    large = shannon_entropy([1.0] * 135, normalize=False)
    assert large > small + 2.0, "entropy must separate 5 authors from 135"
    assert math.exp(small) < math.exp(large)

    # Normalized mode is unchanged and stays inside 0..1.
    assert 0.0 <= shannon_entropy([1.0] * 100, normalize=True) <= 1.0
    assert shannon_entropy([1.0] * 100, normalize=True) == pytest.approx(1.0)
    # A concentrated distribution still scores low.
    assert shannon_entropy([100.0, 1.0, 1.0], normalize=True) < 0.5


def test_author_diversity_separates_a_crowd_from_a_handful():
    """The metric must rank 40 distinct authors above 3, which the clamp prevented."""
    from botsensai.metrics import build_registry

    metric = build_registry().get("mention_author_diversity")
    assert metric is not None

    def ctx_with(author_count: int):
        ctx = context_for("organic", age_seconds=3600.0)
        template = ctx.posts[0]
        ctx.posts = [
            template.model_copy(
                update={"post_id": f"p{i}", "author": f"author{i % author_count}"}
            )
            for i in range(40)
        ]
        return ctx

    few = metric.compute(ctx_with(3))[0]
    many = metric.compute(ctx_with(40))[0]
    assert few is not None and many is not None
    assert many > few, f"40 authors ({many}) must outrank 3 authors ({few})"


# --------------------------------------------------------------------------- #
# promoter profile: fast followers and identity discontinuity (P2-06)
# --------------------------------------------------------------------------- #


def _promoter(ctx, **fields):
    """Attach a promoter profile to a context, defaulting to a clean account.

    Defaults describe an ordinary five-year-old account posting at a steady
    rate, so any test that flags one has flagged something it introduced itself.
    """
    from botsensai.models import Platform, SocialAccount

    created = ctx.as_of - timedelta(days=1825)
    defaults = {
        "platform": Platform.X,
        "handle": "tokenteam",
        "token_key": ctx.token.key,
        "role": "promoter",
        "created_at": created,
        "followers": 10_000,
        "post_count": 9125,  # 5/day for five years
        "timeline_posts": 40,
        "timeline_oldest_at": ctx.as_of - timedelta(days=8),
        "timeline_newest_at": ctx.as_of,
        "observed_at": ctx.as_of,
    }
    defaults.update(fields)
    ctx.accounts = [SocialAccount(**defaults)]
    return ctx


def _discontinuity():
    metric = build_registry().get("identity_discontinuity")
    assert metric is not None
    return metric


def test_identity_discontinuity_flags_a_wiped_archive():
    """Old join date, almost no lifetime posts, dense recent burst."""
    metric = _discontinuity()
    ctx = _promoter(
        context_for("manufactured"),
        post_count=45,  # five years of life, 45 posts total
        timeline_posts=40,  # 40 of which are in the last two days
        timeline_oldest_at=context_for("manufactured").as_of - timedelta(days=2),
    )
    raw, evidence, notes = metric.compute(ctx)
    assert raw is not None, notes
    assert raw > 0.6, f"a wiped archive must score high, got {raw:.3f} ({notes})"
    assert evidence == 40
    # Bearish direction: a high discontinuity must normalize low.
    value = metric.evaluate(ctx)
    assert (value.normalized or 1.0) < 0.35, value.notes


def test_identity_discontinuity_ignores_an_old_prolific_account():
    """The false positive this metric exists to avoid.

    A genuine 2013 account also has an eleven-year gap between its join date
    and the oldest post the timeline endpoint will return, because the endpoint
    returns the head of the timeline and nothing else. The silent-prefix term
    alone therefore fires on every account alive. Only the second term — the
    observed rate against the lifetime rate implied by `post_count` — tells the
    two apart, so this must score at or near zero.
    """
    metric = _discontinuity()
    ctx = _promoter(context_for("organic"))  # 9125 posts over 1825 days
    raw, _, notes = metric.compute(ctx)
    assert raw is not None
    assert raw < 0.05, f"a steady prolific account must not read as wiped: {raw:.3f} ({notes})"

    wiped, _, _ = metric.compute(_promoter(context_for("organic"), post_count=45))
    assert wiped is not None and wiped > raw * 5, (
        "the only difference between the two accounts is the lifetime post count, "
        "so it must be what moves the metric"
    )


def test_identity_discontinuity_is_missing_without_a_profile():
    metric = _discontinuity()
    ctx = context_for("organic")
    assert ctx.accounts == []
    value = metric.evaluate(ctx)
    assert value.normalized is None, "absence must never read as 0.0 — that is bullish here"
    assert value.confidence is Confidence.MISSING
    assert "no profile snapshot" in (value.notes or "")


def test_identity_discontinuity_ignores_engagers():
    """An engager fleet is not the promoter and must not be scored as one."""
    metric = _discontinuity()
    ctx = _promoter(context_for("manufactured"), post_count=45, role="engager")
    value = metric.evaluate(ctx)
    assert value.normalized is None
    assert value.confidence is Confidence.MISSING


def test_identity_discontinuity_is_missing_for_a_young_account():
    """An account younger than the retrievable window has no archive to wipe."""
    metric = _discontinuity()
    ctx = context_for("manufactured")
    ctx = _promoter(
        ctx,
        created_at=ctx.as_of - timedelta(days=3),
        post_count=45,
        timeline_oldest_at=ctx.as_of - timedelta(days=2),
    )
    value = metric.evaluate(ctx)
    assert value.normalized is None
    assert "days old" in (value.notes or "")


def test_identity_discontinuity_is_missing_without_a_join_date():
    metric = _discontinuity()
    ctx = _promoter(context_for("manufactured"), created_at=None)
    raw, evidence, notes = metric.compute(ctx)
    assert raw is None and evidence == 0
    assert "join date" in notes


def test_identity_discontinuity_reads_the_unfiltered_timeline_window():
    """It must use the window recorded at collection time, not `ctx.posts`.

    `ctx.posts` has been filtered to the posts about this token; the promoter's
    off-topic posts are gone. Recounting them would shorten the observed span
    and move the oldest stamp forward, which is the exact shape of a wiped
    archive — so a recount would manufacture the signal on a clean account.
    """
    metric = _discontinuity()
    ctx = _promoter(context_for("organic"))
    baseline, _, _ = metric.compute(ctx)

    # Emptying the post list entirely must not change the answer.
    ctx.posts = []
    assert metric.compute(ctx)[0] == baseline

    # Nor may stuffing it with a dense same-token burst.
    ctx = _promoter(context_for("organic"))
    template = ctx.posts[0]
    ctx.posts = [
        template.model_copy(update={"post_id": f"x{i}", "author": "tokenteam"})
        for i in range(200)
    ]
    assert metric.compute(ctx)[0] == baseline


def test_identity_discontinuity_needs_more_than_two_posts():
    metric = _discontinuity()
    ctx = _promoter(context_for("manufactured"), post_count=45, timeline_posts=2)
    raw, evidence, notes = metric.compute(ctx)
    assert raw is None and evidence == 2
    assert "retrievable posts" in notes


def test_fast_follower_signal_reads_the_stored_promoter():
    """The backtest path. `extra` is a live-sweep artifact and is empty in replay."""
    metric = build_registry().get("purchased_follower_signal")
    assert metric is not None

    ctx = _promoter(context_for("manufactured"), followers=10_000, fast_followers=6_200)
    assert ctx.extra.get("fast_follower_share") is None
    value = metric.evaluate(ctx)
    assert value.raw == pytest.approx(0.62)
    assert (value.normalized or 1.0) < 0.5
    assert "@tokenteam" in (value.notes or "")


def test_fast_follower_signal_ignores_an_engager_fleet():
    """`fast_follower_share` off an engager is a fact about the audience, not the
    promoter, and this metric's thesis is about the promoting account."""
    metric = build_registry().get("purchased_follower_signal")
    assert metric is not None
    ctx = _promoter(
        context_for("manufactured"), role="engager", followers=1_000, fast_followers=900
    )
    value = metric.evaluate(ctx)
    assert value.confidence is Confidence.MISSING
    assert value.normalized is None


def test_identity_discontinuity_takes_the_freshest_promoter_snapshot():
    """Two readings of one account is one account read twice, not two accounts."""
    from botsensai.models import Platform, SocialAccount

    metric = _discontinuity()
    ctx = _promoter(context_for("manufactured"), post_count=45)
    stale = ctx.accounts[0].model_copy(
        update={"post_count": 9125, "observed_at": ctx.as_of - timedelta(hours=6)}
    )
    # Stale reading listed FIRST. Listing it last let a plain `promoters[0]`
    # pass on an ordering the fixture happened to have rather than on the rule.
    ctx.accounts = [stale, ctx.accounts[0]]
    assert isinstance(stale, SocialAccount) and stale.platform is Platform.X
    raw, _, notes = metric.compute(ctx)
    assert raw is not None and raw > 0.6, (
        f"the six-hour-old snapshot must not override the current one: {notes}"
    )


def test_identity_discontinuity_treats_a_zero_post_count_as_the_worst_case():
    """An account whose every post was deleted is the strongest form of the thing
    being measured, so it must score high — and must not divide by zero."""
    metric = _discontinuity()
    ctx = _promoter(context_for("manufactured"), post_count=0)
    raw, _, notes = metric.compute(ctx)
    assert raw is not None, notes
    assert raw > 0.6, f"a zero lifetime post count must read as maximal, got {raw:.3f}"


def test_identity_discontinuity_is_burst_invariant():
    """A prolific account having a busy afternoon is not a wiped archive.

    This killed the first implementation, which compared the rate in the
    retrieved window against the lifetime rate: forty posts inside one hour is
    192x the lifetime rate of a genuinely prolific account, so any account
    posting a thread scored as wiped. Archive coverage does not move at all
    when the same forty posts are compressed into an instant, and that
    invariance is the property being pinned here.
    """
    metric = _discontinuity()
    spread = metric.compute(_promoter(context_for("organic")))[0]

    ctx = context_for("organic")
    instant = ctx.as_of - timedelta(days=8)
    burst = metric.compute(
        _promoter(ctx, timeline_oldest_at=instant, timeline_newest_at=instant)
    )[0]

    assert spread is not None and burst is not None
    assert burst == pytest.approx(spread), (
        f"compressing the window must not move the metric: {spread:.4f} -> {burst:.4f}"
    )
    assert burst < 0.05, f"a prolific account must not read as wiped: {burst:.4f}"


def test_identity_discontinuity_clears_an_account_whose_whole_life_is_visible():
    """Full archive coverage is only damning when it sits behind a silent prefix.

    A small account we can read end to end — forty posts, forty of them
    retrievable, the oldest of them dating to just after it was created — has
    no gap at all. Coverage is 1.0, so the coverage term alone would score it
    as a wiped archive; the silent-prefix term is the only thing that tells it
    apart from a five-year-old handle whose first forty posts are all from last
    week, and this pins that it does.
    """
    metric = _discontinuity()
    ctx = context_for("organic")
    continuous = _promoter(
        ctx,
        created_at=ctx.as_of - timedelta(days=60),
        post_count=40,
        timeline_posts=40,
        timeline_oldest_at=ctx.as_of - timedelta(days=58),
        timeline_newest_at=ctx.as_of,
    )
    raw, _, notes = metric.compute(continuous)
    assert raw is not None
    assert raw < 0.1, f"a continuously-posting account must not read as wiped: {raw:.3f} ({notes})"

    # Same account, same post count, same coverage — only the prefix differs.
    wiped = _promoter(
        context_for("organic"),
        created_at=ctx.as_of - timedelta(days=60),
        post_count=40,
        timeline_posts=40,
        timeline_oldest_at=ctx.as_of - timedelta(days=2),
        timeline_newest_at=ctx.as_of,
    )
    flagged, _, _ = metric.compute(wiped)
    assert flagged is not None and flagged > 0.9, (
        "with coverage held constant, the silent prefix must be what moves the metric"
    )
