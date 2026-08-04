"""Metric framework: context, contract, registry, calibration.

A metric is a pure function from a `MetricContext` to a `MetricValue`. Purity is
the point — it means every metric is unit-testable against a fixture, and it
means the backtester can recompute history exactly rather than trusting stored
values.

Two rules every metric obeys:

* **Never invent data.** If the inputs are absent or too thin, return a value
  with `Confidence.MISSING` and `normalized=None`. Returning 0.0 for "we don't
  know" is the bug that quietly poisons a composite score, because 0.0 is a
  strong bearish claim.
* **Normalized is always "higher is better".** Bearish metrics invert inside
  their own implementation so the scorer never needs to know their polarity.
"""

from __future__ import annotations

import abc
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

from botsensai.models import (
    Confidence,
    Direction,
    HolderRecord,
    Launch,
    MarketSnapshot,
    MetricValue,
    Platform,
    SecurityReport,
    SocialAccount,
    SocialPost,
    TokenRef,
    Trade,
    utcnow,
)
from botsensai.util.logging import get_logger
from botsensai.util.stats import calibrate_logistic, clamp, logistic, percentile_rank

log = get_logger(__name__)


@dataclass
class MetricContext:
    """Everything a metric is allowed to see, frozen at one instant.

    Construction of this object is where point-in-time correctness is enforced;
    metrics themselves cannot reach past it to the database, which is exactly
    why they cannot leak future information.
    """

    token: TokenRef
    as_of: datetime
    launch: Launch | None = None
    snapshots: list[MarketSnapshot] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    holders: list[HolderRecord] = field(default_factory=list)
    security: SecurityReport | None = None
    posts: list[SocialPost] = field(default_factory=list)
    #: Profile snapshots for this token's social accounts, as they read at `as_of`.
    accounts: list[SocialAccount] = field(default_factory=list)

    #: Peer tokens launched in the same window, for cross-sectional normalization.
    peer_values: dict[str, list[float]] = field(default_factory=dict)
    #: Prior history for the deployer, restricted to before `as_of`.
    deployer_history: dict[str, Any] = field(default_factory=dict)
    #: Wallets seen trading before this token existed: {wallet: prior_trade_count}.
    wallet_priors: dict[str, int] = field(default_factory=dict)
    #: Recently launched token names/descriptions, for narrative novelty.
    recent_narratives: list[str] = field(default_factory=list)
    #: Surfaces that failed this sweep; metrics depending on them lower confidence.
    degraded_surfaces: set[str] = field(default_factory=set)
    #: Free-form extras a collector wants to pass through.
    extra: dict[str, Any] = field(default_factory=dict)

    # -- derived views ------------------------------------------------------ #

    @property
    def age_seconds(self) -> float:
        if self.launch is None:
            return 0.0
        return max(0.0, (self.as_of - self.launch.created_at).total_seconds())

    @property
    def promoter(self) -> SocialAccount | None:
        """The token's own named account, never an engager.

        Freshest snapshot wins, because a promoter observed twice in a sweep is
        the same account read twice and the later reading is the current one.
        """
        promoters = [a for a in self.accounts if a.role == "promoter"]
        if not promoters:
            return None
        return max(promoters, key=lambda a: a.observed_at)

    @property
    def latest(self) -> MarketSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def snapshots_within(self, seconds: float) -> list[MarketSnapshot]:
        cutoff = self.as_of - timedelta(seconds=seconds)
        return [s for s in self.snapshots if s.as_of >= cutoff]

    def trades_within(self, seconds: float) -> list[Trade]:
        cutoff = self.as_of - timedelta(seconds=seconds)
        return [t for t in self.trades if t.as_of >= cutoff]

    def posts_within(self, seconds: float, platform: Platform | None = None) -> list[SocialPost]:
        cutoff = self.as_of - timedelta(seconds=seconds)
        out = [p for p in self.posts if p.as_of >= cutoff]
        if platform is not None:
            out = [p for p in out if p.platform == platform]
        return out

    def posts_on(self, platform: Platform) -> list[SocialPost]:
        return [p for p in self.posts if p.platform == platform]

    @property
    def replies(self) -> list[SocialPost]:
        """Comments and replies, as opposed to top-level posts."""
        return [p for p in self.posts if p.parent_id is not None]

    @property
    def top_level_posts(self) -> list[SocialPost]:
        return [p for p in self.posts if p.parent_id is None and not p.is_repost]

    def first_trades(self, n: int) -> list[Trade]:
        """The earliest `n` trades by slot then time. The sniper cohort."""
        return sorted(self.trades, key=lambda t: (t.slot if t.slot is not None else 0, t.as_of))[:n]

    def trades_in_first_seconds(self, seconds: float) -> list[Trade]:
        if self.launch is None:
            return []
        cutoff = self.launch.created_at + timedelta(seconds=seconds)
        return [t for t in self.trades if t.as_of <= cutoff]

    def peer_sample(self, metric_id: str) -> list[float]:
        return self.peer_values.get(metric_id, [])

    def is_degraded(self, *surfaces: str) -> bool:
        return any(s in self.degraded_surfaces for s in surfaces)


class Metric(abc.ABC):
    """One computable signal.

    Subclasses set the class attributes and implement `compute`, returning a raw
    value in native units plus the evidence count behind it. Normalization,
    confidence attenuation and `MetricValue` construction are handled here so
    every metric behaves identically to the scorer.
    """

    id: ClassVar[str] = "unset"
    name: ClassVar[str] = "Unset"
    family: ClassVar[str] = "unassigned"
    thesis: ClassVar[str] = ""
    direction: ClassVar[Direction] = Direction.HIGHER_IS_BULLISH
    sources: ClassVar[tuple[str, ...]] = ()
    #: Seconds after launch before this metric means anything.
    earliest_seconds: ClassVar[float] = 60.0
    #: Minimum number of underlying observations for a full-confidence value.
    min_evidence: ClassVar[int] = 5
    #: Fallback logistic parameters, superseded by calibration when available.
    default_midpoint: ClassVar[float] = 0.0
    default_steepness: ClassVar[float] = 1.0
    #: How a team fakes it, and what we do about it. Documentation with teeth:
    #: every metric must answer this or it does not belong in the composite.
    gameability: ClassVar[str] = ""

    def __init__(self) -> None:
        self._midpoint = self.default_midpoint
        self._steepness = self.default_steepness
        self._calibrated = False

    # -- to implement ------------------------------------------------------- #

    @abc.abstractmethod
    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        """Return (raw_value, evidence_count, note).

        Return `(None, 0, reason)` when the metric cannot be computed. Do not
        return 0.0 to mean "unknown".
        """

    def normalize(self, raw: float, ctx: MetricContext) -> float:
        """Map raw units to 0..1 where 1 is bullish.

        Default: logistic against calibrated parameters, falling back to a
        cross-sectional percentile rank against peers when a peer sample exists,
        which is more robust in a domain whose scale shifts week to week.
        """
        peers = ctx.peer_sample(self.id)
        if len(peers) >= 30:
            value = percentile_rank(raw, peers)
        else:
            value = logistic(raw, self._midpoint, self._steepness)
        if self.direction is Direction.HIGHER_IS_BEARISH:
            value = 1.0 - value
        return clamp(value)

    # -- framework ---------------------------------------------------------- #

    def calibrate(self, samples: Sequence[float]) -> None:
        """Fit logistic parameters from observed history."""
        usable = [s for s in samples if s is not None and math.isfinite(s)]
        if len(usable) < 20:
            return
        self._midpoint, self._steepness = calibrate_logistic(usable)
        self._calibrated = True
        log.debug(
            "metric.calibrated",
            metric=self.id,
            midpoint=round(self._midpoint, 4),
            steepness=round(self._steepness, 4),
            n=len(usable),
        )

    def confidence_for(self, evidence: int, ctx: MetricContext) -> Confidence:
        """Downgrade confidence for thin evidence, immaturity, or dead sources."""
        if evidence <= 0:
            return Confidence.MISSING
        if ctx.age_seconds < self.earliest_seconds:
            return Confidence.LOW
        if self.sources and ctx.is_degraded(*self.sources):
            return Confidence.LOW
        if evidence < self.min_evidence:
            return Confidence.LOW
        if evidence < self.min_evidence * 3:
            return Confidence.MEDIUM
        return Confidence.HIGH

    def evaluate(self, ctx: MetricContext) -> MetricValue:
        """Run the metric with full error containment.

        A metric that raises must not take down a scoring pass; it degrades to
        MISSING and the composite reweights around it.
        """
        try:
            raw, evidence, note = self.compute(ctx)
        except Exception as exc:
            log.warning("metric.error", metric=self.id, token=ctx.token.key, error=str(exc))
            return MetricValue(
                metric_id=self.id,
                token=ctx.token,
                as_of=ctx.as_of,
                raw=None,
                normalized=None,
                confidence=Confidence.MISSING,
                inputs_used=list(self.sources),
                notes=f"error: {type(exc).__name__}: {exc}",
            )

        if raw is None or not math.isfinite(raw):
            return MetricValue(
                metric_id=self.id,
                token=ctx.token,
                as_of=ctx.as_of,
                raw=None,
                normalized=None,
                confidence=Confidence.MISSING,
                inputs_used=list(self.sources),
                notes=note or "insufficient inputs",
            )

        confidence = self.confidence_for(evidence, ctx)
        normalized = None if confidence is Confidence.MISSING else self.normalize(raw, ctx)

        return MetricValue(
            metric_id=self.id,
            token=ctx.token,
            as_of=ctx.as_of,
            raw=float(raw),
            normalized=normalized,
            confidence=confidence,
            inputs_used=list(self.sources),
            notes=note or None,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "family": self.family,
            "thesis": self.thesis,
            "direction": self.direction.value,
            "sources": list(self.sources),
            "earliest_seconds": self.earliest_seconds,
            "min_evidence": self.min_evidence,
            "gameability": self.gameability,
            "calibrated": self._calibrated,
            "midpoint": self._midpoint,
            "steepness": self._steepness,
        }


class MetricRegistry:
    """The metric set. Order is stable so contributions are comparable across runs."""

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}

    def register(self, metric: Metric) -> Metric:
        if metric.id in self._metrics:
            raise ValueError(f"duplicate metric id: {metric.id}")
        if metric.id == "unset":
            raise ValueError(f"{type(metric).__name__} must set a unique `id`")
        if not metric.gameability:
            raise ValueError(
                f"metric '{metric.id}' must document `gameability`; an undocumented "
                "metric cannot be trusted in a composite"
            )
        self._metrics[metric.id] = metric
        return metric

    def register_all(self, metrics: Sequence[Metric]) -> None:
        for m in metrics:
            self.register(m)

    def get(self, metric_id: str) -> Metric | None:
        return self._metrics.get(metric_id)

    def ids(self) -> list[str]:
        return list(self._metrics.keys())

    def families(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for m in self._metrics.values():
            out.setdefault(m.family, []).append(m.id)
        return out

    def __len__(self) -> int:
        return len(self._metrics)

    def __iter__(self) -> Iterator[Metric]:
        return iter(self._metrics.values())

    def __contains__(self, metric_id: object) -> bool:
        return metric_id in self._metrics

    def evaluate_all(self, ctx: MetricContext) -> list[MetricValue]:
        return [m.evaluate(ctx) for m in self._metrics.values()]

    def calibrate_from(self, history: dict[str, Sequence[float]]) -> int:
        """Calibrate every metric that has enough recorded history. Returns count."""
        n = 0
        for metric_id, samples in history.items():
            metric = self._metrics.get(metric_id)
            if metric is not None:
                before = metric._calibrated
                metric.calibrate(samples)
                if metric._calibrated and not before:
                    n += 1
        return n

    def describe(self) -> list[dict[str, Any]]:
        return [m.describe() for m in self._metrics.values()]


def empty_value(metric_id: str, token: TokenRef, as_of: datetime | None = None, note: str = "") -> MetricValue:
    return MetricValue(
        metric_id=metric_id,
        token=token,
        as_of=as_of or utcnow(),
        raw=None,
        normalized=None,
        confidence=Confidence.MISSING,
        notes=note or "no data",
    )


__all__ = ["Metric", "MetricContext", "MetricRegistry", "empty_value"]
