"""Learning the weights instead of guessing them.

The default weights in `composite.py` encode one person's priors about which
signals matter. That is a hypothesis, not a model. This module replaces it with
weights fitted on labelled outcomes, and — more importantly — with an honest
account of how much confidence the fit deserves.

Three deliberate constraints:

* **Weights stay non-negative and normalized within family.** An unconstrained
  regression on this data will happily assign a large negative weight to a
  metric because of noise, producing a model that bets against a signal it does
  not understand. Non-negativity keeps every metric's contribution interpretable.
* **The objective is a ranking objective, not a return regression.** We do not
  need to predict how much a token returns; we need the good ones to sort above
  the bad ones. Fitting to rank is dramatically more stable when the target
  distribution has a tail this heavy.
* **Fitting requires a minimum sample and reports its own inadequacy.** Below the
  threshold the fitter returns the defaults and says so, rather than producing
  authoritative-looking numbers from forty observations.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from botsensai.metrics import MetricRegistry, build_registry
from botsensai.models import MetricValue, Outcome, utcnow
from botsensai.scoring.composite import DEFAULT_FAMILY_WEIGHTS, Weights
from botsensai.util.logging import get_logger
from botsensai.util.stats import clamp

log = get_logger(__name__)

#: Below this many labelled examples, fitting is not meaningful and the fitter
#: refuses rather than producing a number.
MIN_SAMPLES_TO_FIT = 200

#: A decile of nineteen observations is one observation. Below this many samples
#: `top_decile_lift` returns 0.0 — and since the objective is
#: `0.4 * rank + 0.6 * lift`, that silently deletes sixty percent of it rather
#: than failing. Named so a caller with a small holdout can say which of the two
#: terms its numbers actually came from.
TOP_DECILE_MIN_SAMPLES = 20

#: Float dust below which a delta is not treated as a sign. `_objective` is
#: deterministic given fixed weights, so a real negative delta is real; this only
#: keeps `-1e-17` from being announced as a liability.
LIABILITY_TOLERANCE = 1e-9


@dataclass
class TrainingExample:
    """One scored token paired with what actually happened to it."""

    token_key: str
    as_of: datetime
    values: dict[str, float]
    label: float
    weight: float = 1.0

    @classmethod
    def from_values(
        cls,
        token_key: str,
        as_of: datetime,
        values: Sequence[MetricValue],
        outcome: Outcome,
        target: str = "realizable",
    ) -> TrainingExample:
        """Build an example, choosing the target carefully.

        The default target is the *realizable* multiple rather than the peak.
        Training on peak multiple teaches the model to find tokens that spiked
        somewhere no one could have sold, which is a genuinely useless skill.
        """
        if target == "realizable":
            raw = outcome.max_realizable_multiple or outcome.max_multiple_from_t0 or 0.0
        elif target == "peak":
            raw = outcome.max_multiple_from_t0 or 0.0
        elif target == "survival":
            raw = 2.0 if outcome.survived_24h else (1.0 if outcome.survived_1h else 0.0)
        else:
            raise ValueError(f"unknown target {target!r}")

        if outcome.rugged:
            raw = min(raw, 0.2)

        # Log-compress: the difference between 40x and 80x should not dominate
        # the fit relative to the difference between 0.2x and 2x.
        label = clamp(math.log1p(max(0.0, raw)) / math.log1p(20.0))

        return cls(
            token_key=token_key,
            as_of=as_of,
            values={v.metric_id: float(v.normalized) for v in values if v.normalized is not None},
            label=label,
        )


@dataclass
class FitReport:
    """What the fit produced and how much it should be believed."""

    weights: Weights
    n_samples: int
    fitted: bool
    train_rank_correlation: float = 0.0
    holdout_rank_correlation: float = 0.0
    top_decile_lift: float = 0.0
    per_metric_lift: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "fitted": self.fitted,
            "n_samples": self.n_samples,
            "train_rank_correlation": round(self.train_rank_correlation, 4),
            "holdout_rank_correlation": round(self.holdout_rank_correlation, 4),
            "top_decile_lift": round(self.top_decile_lift, 4),
            "weights_version": self.weights.version,
            "warnings": self.warnings,
        }


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation. Used rather than Pearson because the target is a rank."""
    n = len(a)
    if n < 3 or n != len(b):
        return 0.0

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    mean_a = sum(ra) / n
    mean_b = sum(rb) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in ra))
    den_b = math.sqrt(sum((y - mean_b) ** 2 for y in rb))
    if den_a <= 0 or den_b <= 0:
        return 0.0
    return num / (den_a * den_b)


def top_decile_lift(scores: Sequence[float], labels: Sequence[float]) -> float:
    """Mean label in the top-scoring decile divided by the overall mean.

    This is the number that actually matters operationally: the strategy only
    ever trades its highest-scoring candidates, so performance in the tail is
    the whole question and average-case correlation can be misleading.
    """
    if len(scores) < TOP_DECILE_MIN_SAMPLES or len(scores) != len(labels):
        return 0.0
    paired = sorted(zip(scores, labels, strict=True), key=lambda p: p[0], reverse=True)
    k = max(1, len(paired) // 10)
    top_mean = sum(label for _, label in paired[:k]) / k
    overall = sum(labels) / len(labels)
    if overall <= 0:
        return 0.0
    return top_mean / overall


class WeightFitter:
    """Fits within-family metric weights by coordinate ascent on rank correlation.

    Coordinate ascent rather than gradient descent because the objective (rank
    correlation of a normalized weighted sum) is not differentiable in a useful
    way, the parameter count is small, and the method is trivially constrained to
    the non-negative simplex, which is the constraint that keeps the result
    interpretable.
    """

    def __init__(
        self,
        registry: MetricRegistry | None = None,
        iterations: int = 60,
        seed: int = 1337,
    ) -> None:
        self.registry = registry or build_registry()
        self.iterations = iterations
        self.rng = random.Random(seed)
        self.family_of = {m.id: m.family for m in self.registry}

    # -- scoring under a candidate weighting -------------------------------- #

    def _score(self, example: TrainingExample, weights: Weights) -> float:
        by_family: dict[str, list[tuple[str, float]]] = {}
        for metric_id, value in example.values.items():
            family = self.family_of.get(metric_id)
            if family is None:
                continue
            by_family.setdefault(family, []).append((metric_id, value))

        total_weight = 0.0
        acc = 0.0
        for family, members in by_family.items():
            budget = weights.families.get(family, 0.0)
            if budget <= 0:
                continue
            member_weights = [max(0.0, weights.metrics.get(mid, 0.05)) for mid, _ in members]
            member_total = sum(member_weights)
            if member_total <= 0:
                continue
            for (_metric_id, value), w in zip(members, member_weights, strict=True):
                share = (w / member_total) * budget
                acc += share * value
                total_weight += share
        return acc / total_weight if total_weight > 0 else 0.0

    def _objective(self, examples: Sequence[TrainingExample], weights: Weights) -> float:
        scores = [self._score(e, weights) for e in examples]
        labels = [e.label for e in examples]
        rank = spearman(scores, labels)
        lift = top_decile_lift(scores, labels)
        # Blend: rank correlation keeps the whole ordering sane, decile lift
        # optimizes the region we actually trade in.
        return 0.4 * rank + 0.6 * clamp(lift / 3.0)

    # -- fitting ------------------------------------------------------------ #

    def _search(self, train: Sequence[TrainingExample], weights: Weights) -> None:
        """Coordinate ascent over metric weights then family budgets.

        The step halves only when a full pass moves neither, so the search
        spends its iterations where they still buy something and stops when the
        step is too small to matter.
        """
        best = self._objective(train, weights)
        step = 0.5
        for iteration in range(self.iterations):
            best, metrics_improved = self._tune_metrics(train, weights, step, best)
            best, families_improved = self._tune_families(train, weights, best)
            if metrics_improved or families_improved:
                continue
            step *= 0.5
            if step < 0.02:
                log.debug("fit.converged", iteration=iteration)
                break

    def _tune_metrics(
        self, train: Sequence[TrainingExample], weights: Weights, step: float, best: float
    ) -> tuple[float, bool]:
        """One pass over the metric weights in random order."""
        improved = False
        order = [m.id for m in self.registry]
        self.rng.shuffle(order)
        for metric_id in order:
            current = weights.metrics.get(metric_id, 0.05)
            for candidate in (current * (1.0 + step), current * (1.0 - step)):
                candidate = max(0.0, candidate)
                if abs(candidate - current) < 1e-6:
                    continue
                weights.metrics[metric_id] = candidate
                value = self._objective(train, weights)
                if value > best + 1e-6:
                    best = value
                    current = candidate
                    improved = True
                else:
                    weights.metrics[metric_id] = current
        return best, improved

    def _tune_families(
        self, train: Sequence[TrainingExample], weights: Weights, best: float
    ) -> tuple[float, bool]:
        """The same treatment for the family budgets, at a coarser step."""
        improved = False
        for family in list(DEFAULT_FAMILY_WEIGHTS):
            current = weights.families.get(family, 0.1)
            for candidate in (current * 1.25, current * 0.8):
                weights.families[family] = max(0.0, candidate)
                value = self._objective(train, weights)
                if value > best + 1e-6:
                    best = value
                    current = max(0.0, candidate)
                    improved = True
                else:
                    weights.families[family] = current
        return best, improved

    def fit(
        self,
        examples: Sequence[TrainingExample],
        holdout_fraction: float = 0.25,
        version: str | None = None,
    ) -> FitReport:
        warnings: list[str] = []
        n = len(examples)

        if n < MIN_SAMPLES_TO_FIT:
            warnings.append(
                f"only {n} labelled examples; refusing to fit below {MIN_SAMPLES_TO_FIT}. "
                "Default weights returned unchanged — treat them as a hypothesis, not a model."
            )
            return FitReport(
                weights=Weights(version="v0-default"),
                n_samples=n,
                fitted=False,
                warnings=warnings,
            )

        shuffled = list(examples)
        self.rng.shuffle(shuffled)
        split = int(n * (1.0 - holdout_fraction))
        train, holdout = shuffled[:split], shuffled[split:]

        weights = Weights(version=version or f"v{utcnow():%Y%m%d}")
        self._search(train, weights)

        # Renormalize families so the budget sums to one; scores stay in 0..1.
        family_total = sum(weights.families.values())
        if family_total > 0:
            weights.families = {k: v / family_total for k, v in weights.families.items()}

        train_scores = [self._score(e, weights) for e in train]
        train_labels = [e.label for e in train]
        holdout_scores = [self._score(e, weights) for e in holdout]
        holdout_labels = [e.label for e in holdout]

        train_rank = spearman(train_scores, train_labels)
        holdout_rank = spearman(holdout_scores, holdout_labels)
        lift = top_decile_lift(holdout_scores, holdout_labels)

        if holdout_rank < train_rank * 0.5:
            warnings.append(
                f"holdout rank correlation ({holdout_rank:.3f}) is far below train "
                f"({train_rank:.3f}); the fit is overfitting and should not be trusted"
            )
        if holdout_rank <= 0.05:
            warnings.append(
                "holdout rank correlation is effectively zero — these metrics are not "
                "predicting outcomes on this dataset"
            )

        weights.fitted_on = utcnow().isoformat()
        weights.sample_size = n

        report = FitReport(
            weights=weights,
            n_samples=n,
            fitted=True,
            train_rank_correlation=train_rank,
            holdout_rank_correlation=holdout_rank,
            top_decile_lift=lift,
            per_metric_lift=self.per_metric_lift(examples),
            warnings=warnings,
        )
        log.info("fit.done", **report.summary())
        return report

    def per_metric_lift(self, examples: Sequence[TrainingExample]) -> dict[str, float]:
        """Each metric's standalone top-decile lift.

        Reported alongside the fitted weights because a metric with a fitted
        weight but no standalone lift is being carried by correlation with
        something else, and should be viewed with suspicion.
        """
        out: dict[str, float] = {}
        for metric in self.registry:
            pairs = [
                (e.values[metric.id], e.label) for e in examples if metric.id in e.values
            ]
            if len(pairs) < 30:
                continue
            scores = [p[0] for p in pairs]
            labels = [p[1] for p in pairs]
            out[metric.id] = round(top_decile_lift(scores, labels), 4)
        return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))


@dataclass(frozen=True)
class AblationRow:
    """One metric's contribution, with the sample size that produced it.

    `coverage` is not decoration. A delta of zero has two completely different
    meanings — *this metric was scored on every token and moved nothing* and
    *this metric never produced a value at all* — and without the count beside
    it the second is indistinguishable from the first. Reporting an unmeasured
    metric as a neutral one is the same mistake as returning `0.0` from a metric
    to mean "no data".
    """

    metric_id: str
    family: str
    #: Holdout examples that carried a value for this metric.
    coverage: int
    #: Objective with every metric, minus the objective with this one dropped.
    #: Positive means the metric earns its place; negative means removing it
    #: made the model better.
    delta: float

    @property
    def measured(self) -> bool:
        return self.coverage > 0

    @property
    def liability(self) -> bool:
        """Removal improved the objective — and it was actually scored."""
        return self.measured and self.delta < -LIABILITY_TOLERANCE

    @property
    def inert(self) -> bool:
        """Scored on real tokens and worth nothing. Not the same as unmeasured."""
        return self.measured and abs(self.delta) <= LIABILITY_TOLERANCE

    @property
    def state(self) -> str:
        if not self.measured:
            return "not measured"
        if self.liability:
            return "liability"
        if self.inert:
            return "inert"
        return "contributes"

    def describe(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "family": self.family,
            "coverage": self.coverage,
            "delta": round(self.delta, 5),
            "state": self.state,
        }


@dataclass
class AblationReport:
    """Every registered metric's contribution, measured out of sample."""

    rows: list[AblationRow]
    baseline_objective: float
    n_examples: int
    n_train: int
    n_holdout: int
    fitted: bool
    weights_version: str
    #: Token keys on each side of the split. Exposed so a caller can prove the
    #: deltas were measured on tokens the fit never saw, rather than take it on
    #: faith from a docstring.
    train_keys: tuple[str, ...] = ()
    holdout_keys: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def liabilities(self) -> list[AblationRow]:
        return [r for r in self.rows if r.liability]

    @property
    def unmeasured(self) -> list[AblationRow]:
        return [r for r in self.rows if not r.measured]

    @property
    def inert(self) -> list[AblationRow]:
        return [r for r in self.rows if r.inert]

    def summary(self) -> dict[str, Any]:
        return {
            "n_examples": self.n_examples,
            "n_train": self.n_train,
            "n_holdout": self.n_holdout,
            "fitted": self.fitted,
            "weights_version": self.weights_version,
            "baseline_objective": round(self.baseline_objective, 5),
            "metrics": len(self.rows),
            "measured": len(self.rows) - len(self.unmeasured),
            "liabilities": [r.metric_id for r in self.liabilities],
            "warnings": self.warnings,
        }


def _strip(examples: Sequence[TrainingExample], metric_id: str) -> list[TrainingExample]:
    return [
        TrainingExample(
            token_key=e.token_key,
            as_of=e.as_of,
            values={k: v for k, v in e.values.items() if k != metric_id},
            label=e.label,
            weight=e.weight,
        )
        for e in examples
    ]


def _ablation_warnings(report: AblationReport) -> list[str]:
    """The caveats that decide how much of the table means anything."""
    notes: list[str] = []
    if report.n_holdout < TOP_DECILE_MIN_SAMPLES:
        notes.append(
            f"only {report.n_holdout} holdout examples, below the {TOP_DECILE_MIN_SAMPLES} "
            "a decile needs: top_decile_lift is 0.0 for every candidate, so 60% of the "
            "objective is dead and every delta below is the rank term alone"
        )
    if report.unmeasured:
        names = ", ".join(r.metric_id for r in report.unmeasured)
        notes.append(
            f"{len(report.unmeasured)} of {len(report.rows)} metrics produced no value on "
            f"any holdout token, so their 0.00000 means NOT MEASURED, not no effect — "
            f"nothing here judges them: {names}"
        )
    if not report.fitted:
        notes.append(
            "weights were not fitted, so these deltas describe each metric's contribution "
            "under the default hand-set weighting, not under a model"
        )
    return notes


def ablation(
    examples: Sequence[TrainingExample],
    registry: MetricRegistry | None = None,
    seed: int = 1337,
    *,
    holdout_fraction: float = 0.25,
) -> AblationReport:
    """Drop each metric in turn and measure how much the holdout objective falls.

    A metric whose removal does not move the objective is not earning its place,
    and one whose removal *improves* it is actively harmful. Running this
    routinely is what stops the suite from accumulating dead weight.

    The split is the point. Weights are fitted on the train half and every delta
    is then measured on a holdout the fit never saw, because an in-sample
    ablation rewards a metric for the noise it helped memorize — the metric that
    looks most valuable is the one the fit leaned on hardest to overfit. (The
    fitter runs its own inner split of the train half for its own diagnostics;
    that is a different, smaller holdout and it is not this one.)

    Every registered metric gets a row whether or not it was ever scored, and a
    row that was never scored says so rather than reporting a zero delta that
    reads as a verdict.
    """
    reg = registry or build_registry()
    fitter = WeightFitter(reg, seed=seed)
    family_of = {m.id: m.family for m in reg}

    shuffled = list(examples)
    random.Random(seed).shuffle(shuffled)
    split = int(len(shuffled) * (1.0 - holdout_fraction))
    train, holdout = shuffled[:split], shuffled[split:]

    fit = fitter.fit(train)
    baseline = fitter._objective(holdout, fit.weights)

    rows = [
        AblationRow(
            metric_id=metric.id,
            family=family_of.get(metric.id, "unknown"),
            coverage=sum(1 for e in holdout if metric.id in e.values),
            delta=baseline - fitter._objective(_strip(holdout, metric.id), fit.weights),
        )
        for metric in reg
    ]
    # Measured rows first, best contribution at the top. Unmeasured rows sink to
    # the bottom rather than sorting in among the genuine zeros, where they would
    # read as the marginal metrics rather than as the unjudged ones.
    rows.sort(key=lambda r: (r.measured, r.delta), reverse=True)

    report = AblationReport(
        rows=rows,
        baseline_objective=baseline,
        n_examples=len(shuffled),
        n_train=len(train),
        n_holdout=len(holdout),
        fitted=fit.fitted,
        weights_version=fit.weights.version,
        train_keys=tuple(e.token_key for e in train),
        holdout_keys=tuple(e.token_key for e in holdout),
        warnings=list(fit.warnings),
    )
    report.warnings.extend(_ablation_warnings(report))
    log.info("ablation.done", **report.summary())
    return report


__all__ = [
    "LIABILITY_TOLERANCE",
    "MIN_SAMPLES_TO_FIT",
    "TOP_DECILE_MIN_SAMPLES",
    "AblationReport",
    "AblationRow",
    "FitReport",
    "TrainingExample",
    "WeightFitter",
    "ablation",
    "spearman",
    "top_decile_lift",
]
