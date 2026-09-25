"""Quantitative Backtesting Study: Threshold Optimization & Portfolio Congestion.

Replays historical market tapes from data/botsensai.db across:
1. Baselines (Buy Everything, Random Entry, Buy Highest Volume)
2. Default Strategy (Entry Threshold = 0.65)
3. Selective Strategy (Entry Threshold = 0.80)
4. Elite Strategy (Entry Threshold = 0.90)
5. Reserved Capacity Strategy (Entry Threshold = 0.65, but 5 slots reserved for >= 0.90)
"""

from __future__ import annotations

import copy
import json
import statistics
from datetime import datetime, timedelta
from typing import Any

from botsensai.backtest.baselines import BaselineScorer
from botsensai.backtest.engine import BacktestResult, Backtester, TokenTape
from botsensai.config import get_settings
from botsensai.execution.broker import PaperBroker
from botsensai.models import Score, utcnow
from botsensai.scoring.composite import CompositeScorer
from botsensai.store.db import Database


class ThresholdScorer(CompositeScorer):
    """Composite scorer with a configurable entry threshold."""

    def __init__(self, registry: Any, weights: Any, settings: Any, threshold: float = 0.65):
        super().__init__(registry, weights, settings)
        self.custom_threshold = threshold

    def should_enter(self, score: Score) -> tuple[bool, str]:
        if score.vetoed:
            return False, score.explanation or "vetoed"
        if score.coverage < self.settings.scoring.min_coverage:
            return False, f"coverage {score.coverage:.0%} below min"
        if score.composite < self.custom_threshold:
            return False, f"score {score.composite:.3f} below threshold {self.custom_threshold:.3f}"
        return True, score.explanation or "passed"


def run_threshold_study(days: float = 3.0, universe: int = 300, starting_capital: float = 10.0):
    settings = get_settings()
    db = Database(settings.path(settings.db_path))
    end = utcnow()
    start = end - timedelta(days=days)

    print(f"Loading up to {universe} tapes from {start} to {end}...")
    tapes = Backtester.tapes_from_database(db, start, end, limit=universe)
    print(f"Loaded {len(tapes)} replayable tapes.")

    results: dict[str, Any] = {}

    # 1. Baselines
    for mode in ("buy_everything", "random_entry", "buy_highest_volume"):
        print(f"\nEvaluating Baseline: {mode}...")
        b_scorer = BaselineScorer(mode=mode, settings=settings)
        tester = Backtester(settings=settings, scorer=b_scorer)
        res = tester.run(tapes, starting_native=starting_capital)
        results[f"baseline_{mode}"] = res.summary()
        results[f"baseline_{mode}"]["bootstrap_ci"] = res.bootstrap_expectancy_ci()
        print(f"-> {mode}: entered={res.entered}, pnl={res.total_pnl_native:.4f} SOL, win_rate={res.win_rate:.1%}")

    # 2. Threshold Experiments
    thresholds = [0.65, 0.75, 0.85, 0.90, 0.95]
    for th in thresholds:
        name = f"composite_th_{int(th*100)}"
        print(f"\nEvaluating Composite with threshold {th:.2f}...")
        c_scorer = ThresholdScorer(None, None, settings, threshold=th)
        tester = Backtester(settings=settings, scorer=c_scorer)
        res = tester.run(tapes, starting_native=starting_capital)
        results[name] = res.summary()
        results[name]["bootstrap_ci"] = res.bootstrap_expectancy_ci()
        results[name]["top_trades"] = [
            {
                "symbol": t.symbol,
                "token": t.token_key,
                "pnl": round(t.pnl_native, 5),
                "multiple": round(t.multiple, 2),
                "exit": t.exit_reason,
            }
            for t in sorted(res.trades, key=lambda x: x.pnl_native, reverse=True)[:5]
        ]
        print(f"-> th={th:.2f}: entered={res.entered}, pnl={res.total_pnl_native:.4f} SOL, win_rate={res.win_rate:.1%}, profit_factor={res.profit_factor:.2f}")

    # Save summary
    out_file = "data/threshold_study_results.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWrote results to {out_file}")


if __name__ == "__main__":
    run_threshold_study()
