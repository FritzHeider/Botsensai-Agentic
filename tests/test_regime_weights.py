"""Tests for regime-conditional weight sets (P4-04)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from botsensai.cli import app
from botsensai.metrics.base import MetricContext, MetricValue
from botsensai.models import Confidence, Launch, TokenRef
from botsensai.scoring.composite import CompositeScorer, Weights
from botsensai.scoring.fit import (
    MIN_SAMPLES_PER_REGIME,
    TrainingExample,
    fit_regime_weights,
)


def _make_example(
    token_key: str, regime: str, metric_val: float = 0.5, label: float = 0.5
) -> TrainingExample:
    return TrainingExample(
        token_key=token_key,
        as_of=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        values={
            "funder_graph_dispersion": metric_val,
            "deployer_behaviour_now": metric_val,
            "reply_template_ratio": metric_val,
            "realizable_exit_depth": metric_val,
            "organic_media_production_rate": metric_val,
            "narrative_novelty": metric_val,
        },
        label=label,
        regime=regime,
    )


def test_regime_weights_skipped_when_insufficient_samples() -> None:
    # 250 dead examples, but only 50 hot and 10 normal examples
    examples: list[TrainingExample] = []
    for i in range(250):
        examples.append(_make_example(f"solana:dead_{i}", "dead", metric_val=0.4, label=0.3))
    for i in range(50):
        examples.append(_make_example(f"solana:hot_{i}", "hot", metric_val=0.8, label=0.9))
    for i in range(10):
        examples.append(_make_example(f"solana:normal_{i}", "normal", metric_val=0.5, label=0.5))

    report = fit_regime_weights(examples, min_samples_per_regime=MIN_SAMPLES_PER_REGIME)

    assert report.shipped is False
    assert report.skip_reason is not None
    assert "Insufficient samples for regime-conditional fitting" in report.skip_reason
    assert "hot=50" in report.skip_reason
    assert "normal=10" in report.skip_reason
    assert len(report.weights.regimes) == 0


def test_regime_weights_fitted_and_shipped_when_sufficient_samples() -> None:
    examples: list[TrainingExample] = []
    for i in range(210):
        examples.append(_make_example(f"solana:hot_{i}", "hot", metric_val=0.8, label=0.9))
    for i in range(210):
        examples.append(_make_example(f"solana:normal_{i}", "normal", metric_val=0.5, label=0.5))
    for i in range(210):
        examples.append(_make_example(f"solana:dead_{i}", "dead", metric_val=0.2, label=0.1))

    report = fit_regime_weights(examples, min_samples_per_regime=210)

    assert report.shipped is True
    assert report.skip_reason is None
    assert "hot" in report.weights.regimes
    assert "normal" in report.weights.regimes
    assert "dead" in report.weights.regimes
    assert report.weights.regimes["hot"].sample_size > 0
    assert report.weights.regimes["dead"].sample_size > 0


def test_composite_scorer_selects_regime_weights() -> None:
    base_weights = Weights(version="base")
    hot_weights = Weights(
        version="hot_v1",
        families={"narrative": 0.50, "onchain_topology": 0.10},
        metrics={"narrative_novelty": 0.80, "funder_graph_dispersion": 0.10},
    )
    dead_weights = Weights(
        version="dead_v1",
        families={"onchain_topology": 0.60, "narrative": 0.05},
        metrics={"funder_graph_dispersion": 0.90, "narrative_novelty": 0.05},
    )
    base_weights.regimes["hot"] = hot_weights
    base_weights.regimes["dead"] = dead_weights

    scorer = CompositeScorer(weights=base_weights)

    launch = Launch(
        token=TokenRef(mint="11111111111111111111111111111111", symbol="TEST"),
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    )
    ctx = MetricContext(
        token=launch.token,
        as_of=launch.created_at,
        launch=launch,
    )
    values = [
        MetricValue(
            token=launch.token,
            metric_id="narrative_novelty",
            raw=0.9,
            normalized=0.9,
            confidence=Confidence.HIGH,
            as_of=launch.created_at,
        ),
        MetricValue(
            token=launch.token,
            metric_id="funder_graph_dispersion",
            raw=0.2,
            normalized=0.2,
            confidence=Confidence.HIGH,
            as_of=launch.created_at,
        ),
    ]

    score_hot = scorer.score(ctx, values=values, regime="hot")
    score_dead = scorer.score(ctx, values=values, regime="dead")

    # Under hot regime, narrative_novelty dominates the weight
    assert score_hot.regime == "hot"
    assert score_hot.contributions.get("narrative_novelty", 0) > score_hot.contributions.get(
        "funder_graph_dispersion", 0
    )

    # Under dead regime, funder_graph_dispersion has much higher family weight
    assert score_dead.regime == "dead"
    assert (
        score_hot.contributions.get("narrative_novelty", 0)
        > score_dead.contributions.get("narrative_novelty", 0)
    )


def test_regime_weights_serialization(tmp_path: Path) -> None:
    weights = Weights(version="parent_v1")
    weights.regimes["hot"] = Weights(
        version="hot_v1",
        families={"narrative": 0.5},
        metrics={"narrative_novelty": 0.8},
    )
    weights.regimes["dead"] = Weights(
        version="dead_v1",
        families={"onchain_topology": 0.6},
        metrics={"funder_graph_dispersion": 0.9},
    )

    out_file = tmp_path / "weights.json"
    weights.save(out_file)

    loaded = Weights.load(out_file)
    assert loaded.version == "parent_v1"
    assert "hot" in loaded.regimes
    assert loaded.regimes["hot"].version == "hot_v1"
    assert loaded.regimes["hot"].families["narrative"] == 0.5
    assert "dead" in loaded.regimes
    assert loaded.regimes["dead"].metrics["funder_graph_dispersion"] == 0.9


def test_cli_fit_regimes_skipped_report() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["fit", "--synthetic", "--universe", "30", "--regimes"])
    assert result.exit_code == 0
    assert "Insufficient samples for regime-conditional fitting" in result.output
