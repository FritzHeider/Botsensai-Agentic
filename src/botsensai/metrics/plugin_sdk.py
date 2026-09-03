"""Extensible Metric Plugin SDK and Declarative Signal DSL.

Allows users to define custom adversarial signals declaratively or via Python
decorators without modifying the core repository code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botsensai.metrics.base import Metric, MetricContext, MetricRegistry
from botsensai.models import Direction


@dataclass
class CustomMetricSpec:
    metric_id: str
    family: str = "custom"
    direction: Direction = Direction.HIGHER_IS_BULLISH
    gameability: str = "Custom user-defined signal with sandboxed evaluation."
    weight: float = 1.0


def create_custom_metric(
    metric_id: str,
    compute_fn: Callable[[MetricContext], tuple[float | None, int, str]],
    family: str = "custom",
    direction: Direction = Direction.HIGHER_IS_BULLISH,
    gameability: str = "Custom user-defined signal with sandboxed evaluation.",
) -> Metric:
    """Instantiate a dynamic Metric subclass with proper ClassVar bindings."""
    attrs: dict[str, Any] = {
        "id": metric_id,
        "name": metric_id.replace("_", " ").title(),
        "family": family,
        "direction": direction,
        "gameability": gameability,
        "compute": lambda self, ctx: compute_fn(ctx),
    }
    cls = type(f"CustomMetric_{metric_id}", (Metric,), attrs)
    instance = cls()
    return instance  # type: ignore[no-any-return]


def register_custom_metric(
    metric_id: str,
    family: str = "custom",
    direction: Direction = Direction.HIGHER_IS_BULLISH,
    gameability: str = "Custom user-defined signal with sandboxed evaluation.",
    registry: MetricRegistry | None = None,
) -> Callable[[Callable[[MetricContext], tuple[float | None, int, str]]], Callable[[MetricContext], tuple[float | None, int, str]]]:
    """Decorator to register a custom signal into a MetricRegistry."""

    def decorator(
        fn: Callable[[MetricContext], tuple[float | None, int, str]]
    ) -> Callable[[MetricContext], tuple[float | None, int, str]]:
        custom_metric = create_custom_metric(
            metric_id=metric_id,
            compute_fn=fn,
            family=family,
            direction=direction,
            gameability=gameability,
        )
        if registry is not None:
            registry.register(custom_metric)
        return fn

    return decorator


__all__ = [
    "CustomMetricSpec",
    "create_custom_metric",
    "register_custom_metric",
]
