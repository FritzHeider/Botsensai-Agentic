"""The Botsensai metric suite.

Thirty-two metrics across six families, none of which is available from a
standard token-data API. Each one documents the specific evasion it is designed
to survive, because in an adversarial market a metric without a stated
counter-measure is a liability rather than an asset.

Families:

* ``social_authenticity`` — is the visible attention produced by people?
* ``community_production`` — is anyone doing unpaid work for this token?
* ``onchain_topology`` — how many independent actors are actually behind the
  holder set, as opposed to how many addresses there are?
* ``narrative`` — is the idea new, wanted, and findable?
* ``team_credibility`` — what has the deployer done before, and what are they
  doing with their own bag right now?
* ``execution_quality`` — can a position of our size actually be entered and,
  much more importantly, exited?
"""

from __future__ import annotations

from botsensai.metrics.base import Metric, MetricContext, MetricRegistry, empty_value
from botsensai.metrics.community import (
    CommunityContentOriginality,
    CrossPlatformPropagationLag,
    DerivativeRemixDepth,
    OrganicMediaProductionRate,
    UnpaidPromoterShare,
)
from botsensai.metrics.credibility import (
    DeployerBehaviourNow,
    DeployerLineage,
    InsiderSupplyOverhang,
)
from botsensai.metrics.execution import (
    BuyPressureQuality,
    EntryContention,
    PriceStabilityUnderFlow,
    RealizableExitDepth,
)
from botsensai.metrics.narrative import (
    DescriptionSubstance,
    LaunchTimingQuality,
    MetaAlignment,
    NarrativeNovelty,
    TickerContention,
)
from botsensai.metrics.social import (
    ConvictionLanguageShare,
    EngagementDepthRatio,
    EngagerAgeDispersion,
    FollowerEngagementCoherence,
    MentionAuthorDiversity,
    PurchasedFollowerSignal,
    ReplyRhythmNaturalness,
    ReplyTemplateRatio,
    SocialVelocityAcceleration,
)
from botsensai.metrics.topology import (
    BundleSupplyShare,
    EarlyHolderRetention,
    FreshWalletRatio,
    FunderGraphDispersion,
    HolderDistributionHealth,
    SmartWalletParticipation,
    SniperSupplyShare,
)

#: Registration order is stable and defines the order contributions are reported.
METRIC_CLASSES: tuple[type[Metric], ...] = (
    # --- social authenticity -------------------------------------------------
    ReplyTemplateRatio,
    EngagementDepthRatio,
    EngagerAgeDispersion,
    ReplyRhythmNaturalness,
    FollowerEngagementCoherence,
    ConvictionLanguageShare,
    MentionAuthorDiversity,
    SocialVelocityAcceleration,
    PurchasedFollowerSignal,
    # --- community production ------------------------------------------------
    OrganicMediaProductionRate,
    DerivativeRemixDepth,
    CrossPlatformPropagationLag,
    UnpaidPromoterShare,
    CommunityContentOriginality,
    # --- on-chain topology ---------------------------------------------------
    FunderGraphDispersion,
    FreshWalletRatio,
    SniperSupplyShare,
    BundleSupplyShare,
    SmartWalletParticipation,
    EarlyHolderRetention,
    HolderDistributionHealth,
    # --- narrative -----------------------------------------------------------
    NarrativeNovelty,
    MetaAlignment,
    TickerContention,
    LaunchTimingQuality,
    DescriptionSubstance,
    # --- team credibility ----------------------------------------------------
    DeployerLineage,
    DeployerBehaviourNow,
    InsiderSupplyOverhang,
    # --- execution quality ---------------------------------------------------
    RealizableExitDepth,
    EntryContention,
    BuyPressureQuality,
    PriceStabilityUnderFlow,
)


def build_registry(exclude: set[str] | None = None) -> MetricRegistry:
    """Instantiate and register the full metric suite.

    `exclude` takes metric ids, which is how the backtester runs ablation
    studies: drop one metric, refit, and see whether the result moves. A metric
    that does not move it does not belong in the suite.
    """
    excluded = exclude or set()
    registry = MetricRegistry()
    for cls in METRIC_CLASSES:
        if cls.id in excluded:
            continue
        registry.register(cls())
    return registry


def metric_catalogue() -> list[dict]:
    """Machine-readable description of every metric, for docs and the CLI."""
    return build_registry().describe()


__all__ = [
    "METRIC_CLASSES",
    "Metric",
    "MetricContext",
    "MetricRegistry",
    "build_registry",
    "empty_value",
    "metric_catalogue",
]
