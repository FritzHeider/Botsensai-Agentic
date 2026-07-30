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
