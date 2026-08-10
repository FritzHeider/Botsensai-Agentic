"""Null-hypothesis baseline strategies for backtest comparison.

A strategy that cannot beat simple null-hypothesis baselines on the same
universe with the same fill model has no edge.

The three baselines implemented here:
* **buy_everything**: Enters every token at its first decision point.
* **random_entry**: Enters a random subset of tokens at decision points.
* **buy_highest_volume**: Enters tokens whose trading volume is in the upper tier.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from botsensai.backtest.engine import Backtester, BacktestResult, TokenTape
from botsensai.config import Settings, get_settings
from botsensai.metrics import MetricRegistry
from botsensai.metrics.base import MetricContext, MetricValue
from botsensai.models import Score
from botsensai.scoring.composite import CompositeScorer, Weights


def _token_volume(ctx: MetricContext) -> float:
    if ctx.snapshots:
        latest = ctx.snapshots[-1]
        if latest.volume_24h_usd is not None and latest.volume_24h_usd > 0:
            return float(latest.volume_24h_usd)
        if latest.volume_1h_usd is not None and latest.volume_1h_usd > 0:
            return float(latest.volume_1h_usd)
        if latest.volume_5m_usd is not None and latest.volume_5m_usd > 0:
            return float(latest.volume_5m_usd)
    if ctx.trades:
        return sum(
            float(getattr(t, "amount_usd", 0.0) or 0.0)
            or (
                float(getattr(t, "amount_token", 0.0) or 0.0)
                * float(getattr(t, "price_native", 0.0) or 0.0)
                * 150.0
            )
            for t in ctx.trades
        )
    return 0.0


def _compute_volume_threshold(tapes: Sequence[TokenTape]) -> float:
    volumes: list[float] = []
    for tape in tapes:
        for s in tape.snapshots:
            vol = float(s.volume_24h_usd or s.volume_1h_usd or s.volume_5m_usd or 0.0)
            if vol > 0:
                volumes.append(vol)
                break
    if not volumes:
        return 0.0
    volumes.sort()
    return volumes[len(volumes) // 2]


class BaselineScorer(CompositeScorer):
    """Scorer implementing null-hypothesis baseline strategies."""

    def __init__(
        self,
        mode: str,
        settings: Settings | None = None,
        registry: MetricRegistry | None = None,
        seed: int = 1337,
        volume_threshold: float | None = None,
    ) -> None:
        super().__init__(registry=registry, settings=settings)
        if mode not in ("buy_everything", "random_entry", "buy_highest_volume"):
            raise ValueError(f"Unknown baseline mode: {mode}")
        self.mode = mode
        self.seed = seed
        self.volume_threshold = volume_threshold
        self.weights = Weights(version=f"baseline:{mode}")
        self.skip_metric_evaluation = True
        self._entered_keys: set[str] = set()
        self._rng = random.Random(seed)

    def score(
        self,
        ctx: MetricContext,
        values: Sequence[MetricValue] | None = None,
        regime: str | None = None,
    ) -> Score:
        if values is None:
            values = self.registry.evaluate_all(ctx)

        should_buy = self._evaluate_entry(ctx)
        composite = 0.75 if should_buy else 0.0

        return Score(
            token=ctx.token,
            as_of=ctx.as_of,
            composite=composite,
            contributions={},
            weights_version=self.weights.version,
            coverage=1.0,
            regime=regime or "normal",
            vetoes=[],
            metric_values=list(values),
            explanation=f"baseline:{self.mode}",
        )

    def _evaluate_entry(self, ctx: MetricContext) -> bool:
        key = ctx.token.key
        if key in self._entered_keys:
            return False

        if self.mode == "buy_everything":
            return True

        if self.mode == "random_entry":
            return self._rng.random() < 0.35

        if self.mode == "buy_highest_volume":
            vol = _token_volume(ctx)
            thresh = self.volume_threshold if self.volume_threshold is not None else 1_000.0
            return vol >= thresh

        return False

    def should_enter(self, score: Score) -> tuple[bool, str]:
        if score.composite >= self.settings.scoring.entry_threshold:
            self._entered_keys.add(score.token.key)
            return True, f"baseline {self.mode} entry"
        return False, "baseline skip"

    def should_exit(self, score: Score) -> tuple[bool, str]:
        return False, "hold"


def run_baselines(
    tapes: Sequence[TokenTape],
    settings: Settings | None = None,
    starting_native: float = 10.0,
    synthetic: bool = False,
    seed: int = 1337,
) -> dict[str, BacktestResult]:
    """Run all three null-hypothesis baseline strategies over the given universe."""
    resolved_settings = settings or get_settings()
    vol_thresh = _compute_volume_threshold(tapes)
    modes = ["buy_everything", "random_entry", "buy_highest_volume"]
    results: dict[str, BacktestResult] = {}
    for mode in modes:
        scorer = BaselineScorer(
            mode,
            settings=resolved_settings,
            seed=seed,
            volume_threshold=vol_thresh if mode == "buy_highest_volume" else None,
        )
        tester = Backtester(settings=resolved_settings, scorer=scorer)
        results[mode] = tester.run(
            tapes,
            starting_native=starting_native,
            synthetic=synthetic,
        )
    return results


@dataclass
class BaselineComparison:
    """Comparison of composite strategy against null-hypothesis baselines."""

    composite: BacktestResult
    baselines: dict[str, BacktestResult]
    has_edge: bool
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        comp_summary = self.composite.summary()
        base_summaries = {k: v.summary() for k, v in self.baselines.items()}
        return {
            "composite": comp_summary,
            "baselines": base_summaries,
            "has_edge": self.has_edge,
            "notes": self.notes,
        }


def evaluate_baselines(
    composite_result: BacktestResult,
    baselines_results: dict[str, BacktestResult],
) -> BaselineComparison:
    """Compare composite backtest result against null-hypothesis baselines."""
    comp_exp = composite_result.expectancy_native
    comp_pnl = composite_result.total_pnl_native
    beaten: dict[str, bool] = {}
    for mode, b_res in baselines_results.items():
        b_exp = b_res.expectancy_native
        is_better = comp_exp > b_exp or (
            comp_exp == b_exp and comp_pnl > b_res.total_pnl_native
        )
        beaten[mode] = is_better

    has_edge = all(beaten.values())
    notes: list[str] = []
    if not has_edge:
        failed = [m for m, ok in beaten.items() if not ok]
        notes.append(
            f"the composite strategy does not beat all three null-hypothesis baselines "
            f"(failed against {', '.join(failed)}) — it has no edge"
        )
    else:
        notes.append(
            "the composite strategy beat all three null-hypothesis baselines on this universe"
        )

    return BaselineComparison(
        composite=composite_result,
        baselines=baselines_results,
        has_edge=has_edge,
        notes=notes,
    )


__all__ = [
    "BaselineComparison",
    "BaselineScorer",
    "evaluate_baselines",
    "run_baselines",
]
