"""Unit tests for extensible metric plugin SDK."""

from datetime import UTC, datetime

from botsensai.metrics.base import MetricContext, MetricRegistry
from botsensai.metrics.plugin_sdk import register_custom_metric
from botsensai.models import Direction, TokenRef


def test_custom_metric_registration() -> None:
    registry = MetricRegistry()
    initial_count = len(registry)

    @register_custom_metric(
        metric_id="custom_alpha_signal",
        family="community",
        direction=Direction.HIGHER_IS_BULLISH,
        registry=registry,
    )
    def compute_custom(ctx: MetricContext) -> tuple[float | None, int, str]:
        return (0.88, 10, "Custom alpha calculation")

    assert len(registry) == initial_count + 1
    metric = registry.get("custom_alpha_signal")
    assert metric is not None
    assert metric.family == "community"

    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    ctx = MetricContext(
        token=TokenRef(mint="testmint123", name="Test", symbol="TEST"),
        as_of=now,
    )
    result, count, note = metric.compute(ctx)
    assert result == 0.88
    assert count == 10
    assert "Custom alpha" in note
