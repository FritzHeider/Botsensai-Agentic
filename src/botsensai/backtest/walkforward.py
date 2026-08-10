"""Rolling walk-forward evaluation.

`BacktestSettings` has described `train_days`, `test_days`, `step_days` and
`embargo_hours` since the skeleton was written, and nothing consumed any of
them. A single backtest that fits weights on the same population it scores is
not evidence of anything; this module is what turns those four numbers into an
out-of-sample number.

The shape is: fit on a trailing train window, score the next test window with
those weights and nothing else, step forward, repeat. Only the test windows are
ever reported.

**What the embargo is actually for.** Fold membership is decided by
`launch.created_at`, and a fold's train and test `created_at` ranges are
disjoint by construction — so "the same token cannot appear in both" is free and
proves nothing on its own. The leak this market really has is subtler: a token
launched near the end of the train window has no *label* until its outcome
horizon has elapsed, and that horizon lands inside the test window. Fitting on
it means the fit was shown information contemporaneous with the period it is
being graded on. The embargo is therefore a purge on the train side, and it
needs both terms:

    admissible into train  iff  created_at + label_horizon + embargo <= test_start

`label_horizon` is `LabelPolicy.min_age_hours`: the labeller refuses to label a
launch younger than that, so it is genuinely the instant the label becomes
knowable. It is deliberately *not* `Outcome.labeled_at`, which is the wall-clock
moment the batch labeller happened to run — for a corpus labelled in one pass
today that is weeks after the information existed, and would purge every fold to
empty for a reason that has nothing to do with information.

Train and test windows are contiguous (`train_end == test_start`); the purge
does the work rather than a dead calendar gap that would discard the same data
twice.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from botsensai.backtest.engine import (
    BOOTSTRAP_MIN_TRADES,
    Backtester,
    BacktestResult,
    TokenTape,
    TradeRecord,
)
from botsensai.config import BacktestSettings, Settings, get_settings
from botsensai.labeller import LabelPolicy
from botsensai.metrics import MetricRegistry, build_registry
from botsensai.models import utcnow
from botsensai.scoring.composite import Weights
from botsensai.scoring.fit import MIN_SAMPLES_TO_FIT, FitReport, TrainingExample, WeightFitter
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: When a train token's label becomes knowable, in hours after its launch. The
#: labeller's own minimum age, so the two agree by construction.
DEFAULT_LABEL_HORIZON_HOURS = LabelPolicy.min_age_hours

#: Age at which a train token is sampled for its feature vector. One row per
#: token, not one per decision point: sixty correlated rows from one launch
#: would let it outvote fifty-nine other launches in the fit.
DEFAULT_TRAIN_SAMPLE_AGE_SECONDS = 300.0


@dataclass(frozen=True)
class Fold:
    """One train/test split, with the embargo already resolved to an instant."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    #: Latest `created_at` still admissible into this fold's train set. Anything
    #: launched at or after it has a label that resolves too close to the test
    #: window to be used for fitting it.
    train_cutoff: datetime

    def holds_train(self, created_at: datetime) -> bool:
        """Inside the rolling window on the left, before the purge on the right.

        There is deliberately no `< train_end` term. `plan_folds` builds
        `train_cutoff` as `test_start - purge` with `train_end == test_start`
        and a non-negative purge, so the cutoff is at or before `train_end`
        always and a `train_end` test could never be the deciding condition —
        it would read as the boundary doing the work while the purge did it.
        The invariant that makes that true is enforced where it can actually be
        violated: `plan_folds` refuses a negative embargo or label horizon.
        """
        return self.train_start <= created_at < self.train_cutoff

    def holds_test(self, created_at: datetime) -> bool:
        return self.test_start <= created_at < self.test_end

    def describe(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "train": f"{self.train_start.isoformat()} .. {self.train_end.isoformat()}",
            "test": f"{self.test_start.isoformat()} .. {self.test_end.isoformat()}",
            "train_cutoff": self.train_cutoff.isoformat(),
        }


def plan_folds(
    backtest: BacktestSettings,
    start: datetime,
    end: datetime,
    *,
    label_horizon_hours: float = DEFAULT_LABEL_HORIZON_HOURS,
) -> list[Fold]:
    """Rolling folds over `[start, end)`, or `[]` when the window is too short.

    An empty list is a real answer and the caller must report it rather than
    printing a backtest with no folds in it: a window shorter than one
    train+test span cannot produce an out-of-sample number at all.

    A non-positive `step_days` is refused rather than clamped: clamping it to
    some epsilon produces folds that all start at the same instant and a loop
    that does not terminate, which reads as a hang rather than as the
    misconfiguration it is.
    """
    if backtest.step_days <= 0:
        raise ValueError(f"step_days must be positive, got {backtest.step_days}")
    if backtest.train_days <= 0 or backtest.test_days <= 0:
        raise ValueError(
            f"train_days and test_days must be positive, got "
            f"{backtest.train_days} and {backtest.test_days}"
        )
    if backtest.embargo_hours < 0 or label_horizon_hours < 0:
        # A negative purge does not merely widen the train set, it moves the
        # cutoff *past* `train_end` and into the test window — the harness would
        # fit on the very tokens it is about to be graded on, and report the
        # result as out-of-sample. Refused rather than clamped: a negative
        # embargo is always a mistake, and silently reading it as zero would
        # hide the mistake behind a number that looks fine.
        raise ValueError(
            f"embargo_hours and label_horizon_hours must be non-negative, got "
            f"{backtest.embargo_hours} and {label_horizon_hours}"
        )
    train = timedelta(days=backtest.train_days)
    test = timedelta(days=backtest.test_days)
    step = timedelta(days=backtest.step_days)
    purge = timedelta(hours=backtest.embargo_hours + label_horizon_hours)

    folds: list[Fold] = []
    index = 0
    while True:
        test_start = start + train + step * index
        test_end = test_start + test
        if test_end > end:
            break
        folds.append(
            Fold(
                index=index,
                train_start=test_start - train,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
                train_cutoff=test_start - purge,
            )
        )
        index += 1
    return folds


def split_tapes(
    tapes: Sequence[TokenTape], fold: Fold
) -> tuple[list[TokenTape], list[TokenTape]]:
    """Partition a universe into this fold's train and test sets.

    A train tape must also carry a usable label. A tape whose outcome has
    neither a realizable nor a peak multiple is dropped rather than passed on:
    `TrainingExample.from_values` reads
    `max_realizable_multiple or max_multiple_from_t0 or 0.0`, so an unlabelled
    path would enter the fit as a confident zero — the strongest possible
    bearish claim — for want of a t0 price.
    """
    train: list[TokenTape] = []
    test: list[TokenTape] = []
    for tape in tapes:
        created = tape.launch.created_at
        if fold.holds_test(created):
            test.append(tape)
        elif fold.holds_train(created) and _has_label(tape):
            train.append(tape)
    train.sort(key=lambda t: t.launch.created_at)
    test.sort(key=lambda t: t.launch.created_at)
    return train, test


def _has_label(tape: TokenTape) -> bool:
    outcome = tape.outcome
    if outcome is None:
        return False
    return (
        outcome.max_realizable_multiple is not None
        or outcome.max_multiple_from_t0 is not None
    )


@dataclass
class FoldResult:
    """One fold's fit and the out-of-sample run it produced."""

    fold: Fold
    train_size: int
    test_size: int
    fit: FitReport
    result: BacktestResult
    starting_native: float
    ending_native: float

    def summary(self) -> dict[str, Any]:
        return {
            **self.fold.describe(),
            "train_tokens": self.train_size,
            "test_tokens": self.test_size,
            "fitted": self.fit.fitted,
            "weights_version": self.fit.weights.version,
            "holdout_rank_correlation": round(self.fit.holdout_rank_correlation, 4),
            "evaluated": self.result.evaluated,
            "entered": self.result.entered,
            "trades": len(self.result.trades),
            "pnl_native": round(self.result.total_pnl_native, 6),
            "starting_native": round(self.starting_native, 6),
            "ending_native": round(self.ending_native, 6),
        }


@dataclass
class WalkForwardReport:
    """Every fold, plus the pooled out-of-sample result.

    The pooled result is the only number worth quoting, and it is assembled from
    test-window trades exclusively — no fold's training data reaches it.
    """

    folds: list[FoldResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    settings_used: dict[str, Any] = field(default_factory=dict)

    @property
    def trades(self) -> list[TradeRecord]:
        return [t for f in self.folds for t in f.result.trades]

    @property
    def fitted_folds(self) -> int:
        return sum(1 for f in self.folds if f.fit.fitted)

    def pooled(self) -> BacktestResult:
        """All folds' out-of-sample trades as one result."""
        if not self.folds:
            now = utcnow()
            return BacktestResult(
                started_at=now,
                finished_at=now,
                window_start=now,
                window_end=now,
                universe_size=0,
                evaluated=0,
                entered=0,
                notes=list(self.notes),
            )
        evaluated = sum(f.result.evaluated for f in self.folds)
        return BacktestResult(
            started_at=min(f.result.started_at for f in self.folds),
            finished_at=max(f.result.finished_at for f in self.folds),
            window_start=self.folds[0].fold.test_start,
            window_end=self.folds[-1].fold.test_end,
            universe_size=sum(f.test_size for f in self.folds),
            evaluated=evaluated,
            entered=sum(f.result.entered for f in self.folds),
            trades=self.trades,
            account=self._pooled_account(),
            score_distribution=[s for f in self.folds for s in f.result.score_distribution],
            veto_counts=self._pooled_vetoes(),
            metric_coverage=self._pooled_coverage(evaluated),
            absence_reasons=self._pooled_absences(),
            weights_version=f"walkforward:{self.fitted_folds}/{len(self.folds)}-fitted",
            synthetic=any(f.result.synthetic for f in self.folds),
            notes=list(self.notes),
        )

    def _pooled_account(self) -> dict[str, Any]:
        first = self.folds[0]
        last = self.folds[-1]
        return {
            "starting_native": round(first.starting_native, 6),
            "equity_native": round(last.ending_native, 6),
            "total_return": round(
                (last.ending_native - first.starting_native) / first.starting_native, 6
            )
            if first.starting_native > 0
            else 0.0,
        }

    def _pooled_vetoes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for fold in self.folds:
            for veto, count in fold.result.veto_counts.items():
                out[veto] = out.get(veto, 0) + count
        return dict(sorted(out.items()))

    def _pooled_coverage(self, evaluated: int) -> dict[str, float]:
        """Evaluation-weighted coverage. Coverage is hits/evaluated per fold, so
        weighting by each fold's evaluation count recovers the exact pooled
        figure; a plain mean over folds would give a twelve-token fold the same
        say as a two-hundred-token one."""
        if evaluated <= 0:
            return {}
        hits: dict[str, float] = {}
        for fold in self.folds:
            for metric_id, value in fold.result.metric_coverage.items():
                hits[metric_id] = hits.get(metric_id, 0.0) + value * fold.result.evaluated
        return {k: round(v / evaluated, 4) for k, v in sorted(hits.items())}

    def _pooled_absences(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for fold in self.folds:
            for metric_id, reason in fold.result.absence_reasons.items():
                out.setdefault(metric_id, reason)
        return dict(sorted(out.items()))

    def summary(self) -> dict[str, Any]:
        """Pooled headline with the sample size and interval attached.

        The interval and its *absence* are reported as different things. Below
        `BOOTSTRAP_MIN_TRADES` out-of-sample trades `bootstrap_expectancy_ci`
        returns the `(0.0, 0.0)` sentinel, and reading that as an interval makes
        every under-sampled run print "not distinguishable from no edge" — which
        sounds like a measured verdict on the strategy and is really a statement
        that nothing was measured at all.
        """
        pooled = self.pooled()
        out = pooled.summary()
        n_trades = len(pooled.trades)
        out["folds"] = len(self.folds)
        out["fitted_folds"] = self.fitted_folds
        out["out_of_sample"] = True
        out["settings"] = dict(self.settings_used)
        out["fold_table"] = [f.summary() for f in self.folds]
        if n_trades < BOOTSTRAP_MIN_TRADES:
            out["expectancy_ci_95"] = None
            out["warning_ci"] = (
                f"{n_trades} out-of-sample trades is below the {BOOTSTRAP_MIN_TRADES} needed "
                "to bootstrap an interval at all — this run has no confidence interval, "
                "which is not the same as a narrow one"
            )
            return out
        low, high = pooled.bootstrap_expectancy_ci()
        out["expectancy_ci_95"] = [low, high]
        if low <= 0 <= high:
            out["warning_ci"] = (
                "the 95% interval on expectancy spans zero — this walk-forward result "
                "is not distinguishable from no edge"
            )
        return out


class WalkForward:
    """Runs the rolling fit/evaluate loop described in the module docstring."""

    def __init__(
        self,
        settings: Settings | None = None,
        registry: MetricRegistry | None = None,
        *,
        label_horizon_hours: float | None = None,
        train_sample_age_seconds: float = DEFAULT_TRAIN_SAMPLE_AGE_SECONDS,
        decision_interval_seconds: float = 60.0,
        max_decision_age_seconds: float = 3600.0,
        fit_target: str = "realizable",
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or build_registry()
        self.label_horizon_hours = (
            DEFAULT_LABEL_HORIZON_HOURS if label_horizon_hours is None else label_horizon_hours
        )
        self.train_sample_age = train_sample_age_seconds
        self.decision_interval = decision_interval_seconds
        self.max_decision_age = max_decision_age_seconds
        self.fit_target = fit_target

    # -- folds -------------------------------------------------------------- #

    def folds(
        self, tapes: Sequence[TokenTape], start: datetime | None, end: datetime | None
    ) -> list[Fold]:
        if not tapes:
            return []
        window_start = start or min(t.launch.created_at for t in tapes)
        window_end = end or max(
            (t.snapshots[-1].as_of if t.snapshots else t.launch.created_at) for t in tapes
        )
        return plan_folds(
            self.settings.backtest,
            window_start,
            window_end,
            label_horizon_hours=self.label_horizon_hours,
        )

    # -- training ----------------------------------------------------------- #

    def training_examples(self, train: Sequence[TokenTape]) -> list[TrainingExample]:
        """Feature vectors for a fold's train set, one row per token.

        Peer values accumulate in launch order across the fold, which is the
        same rule the replay uses: cross-sectional normalization must never see
        a peer that had not been computed yet, or the fit is quietly told what
        the rest of the cohort was about to do.
        """
        examples: list[TrainingExample] = []
        peer_values: dict[str, list[float]] = {}
        ordered = sorted(train, key=lambda t: t.launch.created_at)
        for tape in ordered:
            when = tape.launch.created_at + timedelta(seconds=self.train_sample_age)
            if tape.snapshot_at(when) is None:
                continue
            ctx = tape.context_at(
                when,
                peer_values=peer_values,
                recent_narratives=Backtester.recent_narratives(ordered, when),
                market_regime=Backtester.compute_regime(ordered, when),
                deployer_history=Backtester._deployer_history(ordered, tape, when),
                extra={
                    "target_position_usd": self.settings.risk.max_position_native * 150.0,
                    "max_impact_pct": self.settings.risk.max_slippage_bps / 10_000.0,
                },
            )
            values = self.registry.evaluate_all(ctx)
            for value in values:
                if value.raw is not None:
                    peer_values.setdefault(value.metric_id, []).append(value.raw)
            outcome = tape.outcome
            if outcome is None:  # pragma: no cover - split_tapes already filtered
                continue
            examples.append(
                TrainingExample.from_values(
                    tape.token.key, when, values, outcome, target=self.fit_target
                )
            )
        return examples

    def fit_fold(self, train: Sequence[TokenTape]) -> FitReport:
        """Fit weights for one fold. Below the sample floor this returns the
        defaults with a warning attached, which is the honest outcome — it does
        not silently reuse the previous fold's fit."""
        examples = self.training_examples(train)
        return WeightFitter(self.registry, seed=self.settings.backtest.seed).fit(examples)

    # -- run ---------------------------------------------------------------- #

    def run(
        self,
        tapes: Sequence[TokenTape],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        starting_native: float | None = None,
        synthetic: bool = False,
    ) -> WalkForwardReport:
        capital = (
            starting_native
            if starting_native is not None
            else self.settings.backtest.initial_capital_native
        )
        report = WalkForwardReport(settings_used=self._settings_used())
        folds = self.folds(tapes, start, end)
        if not folds:
            report.notes.append(
                f"the window is shorter than one train+test span "
                f"({self.settings.backtest.train_days} + {self.settings.backtest.test_days} days); "
                "no out-of-sample fold could be built and there is nothing to report"
            )
            return report

        unfitted = 0
        for fold in folds:
            train, test = split_tapes(tapes, fold)
            fit = self.fit_fold(train)
            if not fit.fitted:
                unfitted += 1
            result = self._run_fold(fold, test, fit.weights, capital, synthetic)
            ending = float(result.account.get("equity_native", capital) or capital)
            report.folds.append(
                FoldResult(
                    fold=fold,
                    train_size=len(train),
                    test_size=len(test),
                    fit=fit,
                    result=result,
                    starting_native=capital,
                    ending_native=ending,
                )
            )
            # Capital compounds across folds: the test windows are contiguous
            # and disjoint, so resetting to the initial stake each fold would
            # draw an equity curve nobody could have traded.
            capital = max(ending, 0.0)

        report.notes.extend(self._notes(report, unfitted))
        log.info(
            "walkforward.done",
            folds=len(report.folds),
            fitted=report.fitted_folds,
            trades=len(report.trades),
        )
        return report

    def _run_fold(
        self,
        fold: Fold,
        test: Sequence[TokenTape],
        weights: Weights,
        capital: float,
        synthetic: bool,
    ) -> BacktestResult:
        tester = Backtester(
            self.settings,
            self.registry,
            weights,
            decision_interval_seconds=self.decision_interval,
            max_decision_age_seconds=self.max_decision_age,
        )
        return tester.run(
            test,
            start=fold.test_start,
            end=fold.test_end,
            starting_native=capital,
            synthetic=synthetic,
        )

    def _settings_used(self) -> dict[str, Any]:
        bt = self.settings.backtest
        return {
            "train_days": bt.train_days,
            "test_days": bt.test_days,
            "step_days": bt.step_days,
            "embargo_hours": bt.embargo_hours,
            "label_horizon_hours": self.label_horizon_hours,
            "train_sample_age_seconds": self.train_sample_age,
        }

    @staticmethod
    def _notes(report: WalkForwardReport, unfitted: int) -> list[str]:
        notes: list[str] = []
        if unfitted:
            train_sizes = [f.train_size for f in report.folds]
            notes.append(
                f"{unfitted} of {len(report.folds)} folds had fewer than {MIN_SAMPLES_TO_FIT} "
                f"labelled training examples (largest train fold: {max(train_sizes)} tokens) "
                "and ran on default weights — those folds test the harness, not a fitted model"
            )
        trades = report.trades
        if len(trades) < 30:
            notes.append(
                f"{len(trades)} out-of-sample trades across all folds; far below the sample "
                "needed for any conclusion about edge"
            )
        if trades:
            per_fold = [len(f.result.trades) for f in report.folds]
            notes.append(
                f"trades per fold: min {min(per_fold)}, median "
                f"{statistics.median(per_fold):.1f}, max {max(per_fold)}"
            )
        return notes


__all__ = [
    "DEFAULT_LABEL_HORIZON_HOURS",
    "DEFAULT_TRAIN_SAMPLE_AGE_SECONDS",
    "Fold",
    "FoldResult",
    "WalkForward",
    "WalkForwardReport",
    "plan_folds",
    "split_tapes",
]
