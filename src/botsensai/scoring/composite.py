"""Composite scoring: turning 32 metrics into one decision.

The hard problems in combining metrics are not arithmetic, they are:

* **Missing data must not read as bad news.** A metric that could not be computed
  is dropped and the remaining weights are renormalized, with the resulting
  `coverage` reported so the caller can refuse to act on a thin read. Treating an
  absent metric as 0.0 is the single most common way a composite score becomes
  actively harmful.
* **Correlated metrics must not vote twice.** Eight social-authenticity metrics
  that all fire on the same reply farm are one observation, not eight. Weights
  are allocated at the *family* level first and distributed within families
  second, which caps how much any one phenomenon can move the total.
* **Some facts are not tradeable at any score.** A live mint authority is not a
  0.2 penalty, it is a refusal. Vetoes short-circuit the composite entirely.
* **Weights must be learned, not guessed.** Hand-set weights encode the author's
  priors, which is exactly what a backtest is supposed to test. The default
  weights here are explicitly a starting point that `scoring.fit` replaces with
  values fitted on labelled outcomes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from botsensai.config import ScoringSettings, Settings, get_settings
from botsensai.metrics import MetricRegistry, build_registry
from botsensai.metrics.base import MetricContext
from botsensai.models import (
    Confidence,
    CurveStage,
    MetricValue,
    Score,
    SecurityReport,
    VetoReason,
)
from botsensai.util.logging import get_logger
from botsensai.util.stats import clamp

log = get_logger(__name__)

#: Confidence multipliers. A LOW-confidence metric still votes, but quietly.
CONFIDENCE_WEIGHT: dict[Confidence, float] = {
    Confidence.VERIFIED: 1.0,
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.7,
    Confidence.LOW: 0.35,
    Confidence.STALE: 0.15,
    Confidence.MISSING: 0.0,
}

#: Family-level budget. Allocated at family level so that a family with eight
#: correlated members cannot outvote a family with three uncorrelated ones.
DEFAULT_FAMILY_WEIGHTS: dict[str, float] = {
    "onchain_topology": 0.28,
    "team_credibility": 0.20,
    "social_authenticity": 0.18,
    "execution_quality": 0.15,
    "community_production": 0.12,
    "narrative": 0.07,
}

#: Within-family weights. Anything unlisted gets an equal share of the remainder.
DEFAULT_METRIC_WEIGHTS: dict[str, float] = {
    # onchain_topology
    "funder_graph_dispersion": 0.22,
    "sniper_supply_share": 0.18,
    "bundle_supply_share": 0.18,
    "early_holder_retention": 0.16,
    "holder_distribution_health": 0.12,
    "fresh_wallet_ratio": 0.08,
    "smart_wallet_participation": 0.06,
    # team_credibility
    "deployer_behaviour_now": 0.45,
    "deployer_lineage": 0.35,
    "insider_supply_overhang": 0.20,
    # social_authenticity — note that `purchased_follower_signal` is weighted
    # above the inferred proxies on purpose. It is X's own classification of the
    # follower base rather than an inference from an engagement ratio, and direct
    # evidence should outrank a behavioural side-effect of the same thing. It is
    # also far more often absent, which the confidence machinery handles by
    # renormalizing within the family.
    "reply_template_ratio": 0.22,
    "purchased_follower_signal": 0.18,
    "engager_age_dispersion": 0.14,
    "engagement_depth_ratio": 0.12,
    "social_velocity_acceleration": 0.12,
    "reply_rhythm_naturalness": 0.10,
    "mention_author_diversity": 0.07,
    "conviction_language_share": 0.03,
    "follower_engagement_coherence": 0.02,
    # execution_quality
    "realizable_exit_depth": 0.40,
    "buy_pressure_quality": 0.28,
    "price_stability_under_flow": 0.20,
    "entry_contention": 0.12,
    # community_production
    "organic_media_production_rate": 0.32,
    "cross_platform_propagation_lag": 0.26,
    "unpaid_promoter_share": 0.18,
    "derivative_remix_depth": 0.14,
    "community_content_originality": 0.10,
    # narrative
    "narrative_novelty": 0.34,
    "meta_alignment": 0.26,
    "launch_timing_quality": 0.20,
    "ticker_contention": 0.12,
    "description_substance": 0.08,
}

#: Regime multipliers applied to family budgets. In a hot market, narrative and
#: social spread matter more; in a dead one, only survivability does.
REGIME_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "hot": {
        "narrative": 1.6,
        "community_production": 1.4,
        "social_authenticity": 1.2,
        "onchain_topology": 0.9,
        "execution_quality": 0.9,
    },
    "normal": {},
    "dead": {
        "narrative": 0.5,
        "community_production": 0.7,
        "social_authenticity": 0.9,
        "onchain_topology": 1.2,
        "team_credibility": 1.3,
        "execution_quality": 1.3,
    },
}


@dataclass
class Weights:
    """A complete weighting, versioned so scores stay comparable across changes."""

    version: str = "v0"
    families: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FAMILY_WEIGHTS))
    metrics: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_METRIC_WEIGHTS))
    fitted_on: str | None = None
    sample_size: int = 0
    regimes: dict[str, Weights] = field(default_factory=dict)

    def get_weights_for_regime(self, regime: str) -> Weights:
        if self.regimes and regime in self.regimes:
            return self.regimes[regime]
        return self

    def to_dict(self) -> dict[str, Any]:
        out = {
            "version": self.version,
            "families": self.families,
            "metrics": self.metrics,
            "fitted_on": self.fitted_on,
            "sample_size": self.sample_size,
        }
        if self.regimes:
            out["regimes"] = {k: v.to_dict() for k, v in self.regimes.items()}
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Weights:
        regimes_raw = data.get("regimes") or {}
        regimes = {k: cls.from_dict(v) for k, v in regimes_raw.items()} if regimes_raw else {}
        return cls(
            version=data.get("version", "v0"),
            families=dict(data.get("families") or DEFAULT_FAMILY_WEIGHTS),
            metrics=dict(data.get("metrics") or DEFAULT_METRIC_WEIGHTS),
            fitted_on=data.get("fitted_on"),
            sample_size=int(data.get("sample_size", 0)),
            regimes=regimes,
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Weights:
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))


def _over(value: MetricValue, threshold: float) -> bool:
    """Whether a metric produced a reading above `threshold`. Absent is not above."""
    return value.raw is not None and value.raw > threshold


def _contract_vetoes(sec: SecurityReport) -> list[VetoReason]:
    """Vetoes readable off the contract itself, independent of market state."""
    vetoes: list[VetoReason] = []
    if sec.mint_authority_revoked is False:
        vetoes.append(VetoReason.MINT_AUTHORITY_LIVE)
    if sec.freeze_authority_revoked is False:
        vetoes.append(VetoReason.FREEZE_AUTHORITY_LIVE)
    if sec.transfer_fee_bps is not None and sec.transfer_fee_bps > 0:
        vetoes.append(VetoReason.TRANSFER_TAX_PRESENT)
    return vetoes


def _lp_not_burned(sec: SecurityReport, ctx: MetricContext) -> bool:
    """Only meaningful once the token has actually graduated to a pool."""
    if sec.lp_burned_share is None or sec.lp_burned_share >= 0.5:
        return False
    pool = ctx.latest
    return pool is not None and pool.stage is CurveStage.GRADUATED


class VetoEngine:
    """Hard refusals, evaluated before and independently of the score.

    These are deliberately not part of the weighted sum. A token with a live mint
    authority is not 15% worse than one without; it is a category of thing we do
    not buy. Encoding that as a weight lets a sufficiently attractive score
    override it, which is precisely the failure this class exists to prevent.
    """

    def __init__(self, settings: ScoringSettings | None = None) -> None:
        self.settings = settings or ScoringSettings()

    def evaluate(
        self,
        ctx: MetricContext,
        values: Sequence[MetricValue],
        security: SecurityReport | None = None,
        risk_min_liquidity: float = 5_000.0,
        risk_min_age: float = 45.0,
        kill_switch: bool = False,
    ) -> list[VetoReason]:
        if kill_switch:
            return [VetoReason.KILL_SWITCH]

        vetoes: list[VetoReason] = []
        sec = security or ctx.security
        if sec is not None:
            vetoes.extend(_contract_vetoes(sec))
            vetoes.extend(self._supply_vetoes(sec, ctx))
            if sec.dev_sold:
                vetoes.append(VetoReason.DEV_ALREADY_SOLD)

        # Deployer history, restricted to what was knowable at decision time.
        rug_count = int(ctx.deployer_history.get("rug_count", 0) or 0)
        if rug_count >= self.settings.veto_deployer_rug_count:
            vetoes.append(VetoReason.DEPLOYER_PRIOR_RUGS)

        # Metric-derived vetoes: extreme readings that the weighted sum would
        # merely dilute.
        by_id = {v.metric_id: v for v in values}
        vetoes.extend(self._social_vetoes(by_id))
        vetoes.extend(self._supply_metric_vetoes(by_id))
        vetoes.extend(self._risk_vetoes(ctx, by_id, risk_min_liquidity, risk_min_age))

        # A reason reached by two routes — a security report and the metric that
        # measures the same thing — is one veto, not two. Deduplicating once here
        # is why the individual checks do not each have to ask what came before.
        return list(dict.fromkeys(vetoes))

    def _supply_vetoes(self, sec: SecurityReport, ctx: MetricContext) -> list[VetoReason]:
        """Vetoes from how the supply is distributed, per the security report."""
        vetoes: list[VetoReason] = []
        if _lp_not_burned(sec, ctx):
            vetoes.append(VetoReason.LP_NOT_BURNED)
        if self._top10_extreme(sec, ctx):
            vetoes.append(VetoReason.HOLDER_CONCENTRATION_EXTREME)
        if sec.insider_share is not None and sec.insider_share > self.settings.veto_insider_share:
            vetoes.append(VetoReason.INSIDER_SUPPLY_EXCESSIVE)
        if sec.bundled_share is not None and sec.bundled_share > self.settings.veto_bundle_share:
            vetoes.append(VetoReason.BUNDLE_SUPPLY_EXCESSIVE)
        return vetoes

    def _top10_extreme(self, sec: SecurityReport, ctx: MetricContext) -> bool:
        """Whether top-10 concentration is damning *for this curve stage*.

        Concentration is structurally normal on a young bonding curve — there are
        simply not many holders yet — and becomes damning only once the token has
        graduated and had time to distribute. Applying one threshold to both
        stages rejects every early token, which is exactly the population we are
        here to trade.
        """
        if sec.top10_share is None:
            return False
        latest = ctx.latest
        stage = latest.stage if latest is not None else None
        early = stage is not None and stage in (
            CurveStage.BONDING,
            CurveStage.NEAR_GRADUATION,
        )
        threshold = (
            min(0.90, self.settings.veto_top10_share + 0.30)
            if early
            else self.settings.veto_top10_share
        )
        return sec.top10_share > threshold

    def _social_vetoes(self, by_id: dict[str, MetricValue]) -> list[VetoReason]:
        template = by_id.get("reply_template_ratio")
        if (
            template is not None
            and template.raw is not None
            and template.confidence not in (Confidence.MISSING, Confidence.LOW)
            and template.raw >= self.settings.veto_inauthenticity
        ):
            return [VetoReason.SOCIAL_ENGAGEMENT_INAUTHENTIC]
        return []

    def _supply_metric_vetoes(self, by_id: dict[str, MetricValue]) -> list[VetoReason]:
        """The metric-measured counterparts of the security report's supply vetoes."""
        vetoes: list[VetoReason] = []
        overhang = by_id.get("insider_supply_overhang")
        if overhang is not None and _over(overhang, self.settings.veto_insider_share):
            vetoes.append(VetoReason.INSIDER_SUPPLY_EXCESSIVE)
        bundle = by_id.get("bundle_supply_share")
        if bundle is not None and _over(bundle, self.settings.veto_bundle_share):
            vetoes.append(VetoReason.BUNDLE_SUPPLY_EXCESSIVE)
        return vetoes

    @staticmethod
    def _risk_vetoes(
        ctx: MetricContext,
        by_id: dict[str, MetricValue],
        risk_min_liquidity: float,
        risk_min_age: float,
    ) -> list[VetoReason]:
        vetoes: list[VetoReason] = []
        depth = by_id.get("realizable_exit_depth")
        if depth is not None and depth.raw is not None and depth.raw < 1.0:
            # Cannot exit the intended size. Nothing else matters.
            vetoes.append(VetoReason.LIQUIDITY_BELOW_FLOOR)

        latest = ctx.latest
        if (
            latest is not None
            and latest.liquidity_usd is not None
            and latest.liquidity_usd < risk_min_liquidity
        ):
            vetoes.append(VetoReason.LIQUIDITY_BELOW_FLOOR)

        if ctx.age_seconds < risk_min_age:
            vetoes.append(VetoReason.AGE_BELOW_FLOOR)
        return vetoes


class CompositeScorer:
    """Combines metric values into a single 0..1 decision score."""

    def __init__(
        self,
        registry: MetricRegistry | None = None,
        weights: Weights | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or build_registry()
        self.weights = weights or self._load_weights()
        self.vetoes = VetoEngine(self.settings.scoring)
        self._family_of = {m.id: m.family for m in self.registry}

    def _load_weights(self) -> Weights:
        path = self.settings.scoring.weights_path
        if path:
            return Weights.load(self.settings.path(path))
        return Weights()

    # -- regime ------------------------------------------------------------- #

    @staticmethod
    def classify_regime(
        market: dict[str, Any], settings: ScoringSettings | None = None
    ) -> str:
        """Label the current market so weights can be conditioned on it.

        Graduation rate is the cleanest available proxy for whether launches are
        working at all right now, and it swings by an order of magnitude across a
        week. A weighting fitted across all regimes is fitted to none of them.

        The thresholds come from config rather than being hardcoded because the
        base rate is strongly non-stationary — it fell by roughly two-thirds
        between late 2024 and late 2025. Constants baked in against the old rate
        would classify every day since as "dead", which silently switches the
        strategy off rather than adapting it.
        """
        if not market:
            return "unknown"
        rate = market.get("graduation_rate_24h")
        if rate is None:
            return "unknown"
        thresholds = settings or ScoringSettings()
        rate = float(rate)
        if rate >= thresholds.regime_hot_graduation_rate:
            return "hot"
        if rate <= thresholds.regime_dead_graduation_rate:
            return "dead"
        return "normal"

    def effective_family_weights(self, regime: str) -> dict[str, float]:
        if self.weights.regimes and regime in self.weights.regimes:
            return dict(self.weights.regimes[regime].families)
        adjustments = REGIME_ADJUSTMENTS.get(regime, {})
        adjusted = {
            family: base * adjustments.get(family, 1.0)
            for family, base in self.weights.families.items()
        }
        total = sum(adjusted.values())
        if total <= 0:
            return dict(self.weights.families)
        return {k: v / total for k, v in adjusted.items()}

    # -- scoring ------------------------------------------------------------ #

    def score(
        self,
        ctx: MetricContext,
        values: Sequence[MetricValue] | None = None,
        regime: str | None = None,
    ) -> Score:
        """Evaluate every metric and fold the results into one decision."""
        if values is None:
            values = self.registry.evaluate_all(ctx)

        market = ctx.extra.get("market_regime", {})
        resolved_regime = regime or self.classify_regime(market, self.settings.scoring)
        family_weights = self.effective_family_weights(resolved_regime)
        active_weights = self.weights.get_weights_for_regime(resolved_regime)

        # Group usable values by family so weights can be renormalized within
        # each family independently. This is what stops a family whose data
        # source is down from silently handing its budget to another family.
        by_family: dict[str, list[tuple[MetricValue, float]]] = {}
        usable_count = 0
        for value in values:
            family = self._family_of.get(value.metric_id, "unassigned")
            if not value.usable or value.normalized is None:
                by_family.setdefault(family, [])
                continue
            confidence_factor = CONFIDENCE_WEIGHT.get(value.confidence, 0.0)
            if confidence_factor <= 0:
                by_family.setdefault(family, [])
                continue
            base = active_weights.metrics.get(value.metric_id, 0.0)
            if base <= 0:
                # Unlisted metric: give it a small default so a newly added
                # metric contributes something before weights are refitted.
                base = 0.05
            by_family.setdefault(family, []).append((value, base * confidence_factor))
            usable_count += 1

        total_weight = 0.0
        weighted_sum = 0.0
        contributions: dict[str, float] = {}

        for family, budget in family_weights.items():
            members = by_family.get(family, [])
            if not members:
                continue
            member_total = sum(w for _, w in members)
            if member_total <= 0:
                continue
            for value, weight in members:
                share = (weight / member_total) * budget
                contribution = share * float(value.normalized or 0.0)
                contributions[value.metric_id] = round(contribution, 6)
                weighted_sum += contribution
                total_weight += share

        composite = clamp(weighted_sum / total_weight) if total_weight > 0 else 0.0
        coverage = usable_count / max(1, len(values))

        veto_list = self.vetoes.evaluate(
            ctx,
            values,
            risk_min_liquidity=self.settings.risk.min_liquidity_usd,
            risk_min_age=self.settings.risk.min_token_age_seconds,
            kill_switch=self.settings.risk.kill_switch,
        )

        score = Score(
            token=ctx.token,
            as_of=ctx.as_of,
            composite=composite,
            contributions=contributions,
            weights_version=self.weights.version,
            coverage=coverage,
            regime=resolved_regime,
            vetoes=veto_list,
            metric_values=list(values),
        )
        score.explanation = self.explain(score)
        return score

    # -- explanation -------------------------------------------------------- #

    def explain(self, score: Score, top_n: int = 6) -> str:
        """Human-readable rationale. Feeds both the CLI and the content generator."""
        if score.vetoes:
            reasons = ", ".join(v.value.replace("_", " ") for v in score.vetoes)
            return f"REJECTED ({reasons})"

        ranked = sorted(score.contributions.items(), key=lambda kv: kv[1], reverse=True)
        drivers = ranked[:top_n]
        by_id = {v.metric_id: v for v in score.metric_values}

        parts: list[str] = [
            f"score {score.composite:.3f} in a {score.regime} regime "
            f"(coverage {score.coverage:.0%})"
        ]
        for metric_id, contribution in drivers:
            value = by_id.get(metric_id)
            if value is None or value.normalized is None:
                continue
            metric = self.registry.get(metric_id)
            label = metric.name if metric else metric_id
            parts.append(
                f"{label} {value.normalized:.2f} (+{contribution:.3f})"
            )

        weakest = [
            (mid, c)
            for mid, c in sorted(score.contributions.items(), key=lambda kv: kv[1])
            if (by_id.get(mid) and (by_id[mid].normalized or 1.0) < 0.35)
        ][:2]
        for metric_id, _ in weakest:
            metric = self.registry.get(metric_id)
            label = metric.name if metric else metric_id
            value = by_id.get(metric_id)
            if value and value.normalized is not None:
                parts.append(f"weak: {label} {value.normalized:.2f}")

        missing = [v.metric_id for v in score.metric_values if not v.usable]
        if missing:
            parts.append(f"{len(missing)} metrics unavailable")

        return "; ".join(parts)

    def should_enter(self, score: Score) -> tuple[bool, str]:
        """Final gate. Returns (decision, reason)."""
        if score.vetoed:
            return False, score.explanation or "vetoed"
        if score.coverage < self.settings.scoring.min_coverage:
            return False, (
                f"coverage {score.coverage:.0%} below minimum "
                f"{self.settings.scoring.min_coverage:.0%}"
            )
        if score.composite < self.settings.scoring.entry_threshold:
            return False, (
                f"score {score.composite:.3f} below threshold "
                f"{self.settings.scoring.entry_threshold:.3f}"
            )
        return True, score.explanation or "passed"

    def should_exit(self, score: Score) -> tuple[bool, str]:
        if score.vetoed:
            return True, score.explanation or "veto triggered while in position"
        if score.composite < self.settings.scoring.exit_threshold:
            return True, (
                f"score {score.composite:.3f} fell below exit threshold "
                f"{self.settings.scoring.exit_threshold:.3f}"
            )
        return False, "hold"


def kelly_fraction(win_prob: float, win_multiple: float, loss_fraction: float = 1.0) -> float:
    """Fractional Kelly sizing, capped hard.

    Full Kelly is wrong here for two reasons: the probability estimate is far
    less reliable than Kelly assumes, and the loss distribution has a fat left
    tail (a rug loses everything, not the modelled fraction). Quarter Kelly with
    a hard cap is the defensible compromise.
    """
    if win_prob <= 0 or win_multiple <= 0 or loss_fraction <= 0:
        return 0.0
    b = win_multiple - 1.0
    if b <= 0:
        return 0.0
    edge = (win_prob * b - (1.0 - win_prob) * loss_fraction) / b
    return clamp(edge * 0.25, 0.0, 0.05)


def score_to_size(
    score: Score,
    max_position_native: float,
    assumed_win_multiple: float = 6.0,
    stop_loss_fraction: float = 0.45,
) -> float:
    """Translate a composite score into a position size in native units.

    Three deliberate choices here. The assumed win multiple defaults to 6x rather
    than something modest, because the return distribution in this market is
    violently fat-tailed and a strategy sized for 3x winners will never survive
    the base rate of total losses. The loss side uses the configured stop rather
    than assuming total loss, since the exit logic does in fact cut. And the
    mapping is convex — sizing scales with the *square* of the excess over the
    midpoint — so a marginal signal takes a token position and only a strong one
    takes full size.
    """
    if score.vetoed or score.composite <= 0:
        return 0.0
    excess = max(0.0, score.composite - 0.5) / 0.5
    if excess <= 0:
        return 0.0
    implied_win_prob = clamp(0.10 + 0.35 * excess)
    fraction = kelly_fraction(implied_win_prob, assumed_win_multiple, stop_loss_fraction)
    if fraction <= 0:
        return 0.0
    confidence_scale = clamp(score.coverage) ** 2
    convexity = excess**2
    return max_position_native * clamp(fraction / 0.05) * convexity * confidence_scale


__all__ = [
    "CONFIDENCE_WEIGHT",
    "DEFAULT_FAMILY_WEIGHTS",
    "DEFAULT_METRIC_WEIGHTS",
    "REGIME_ADJUSTMENTS",
    "CompositeScorer",
    "VetoEngine",
    "Weights",
    "kelly_fraction",
    "score_to_size",
]
