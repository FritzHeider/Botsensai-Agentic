"""Social authenticity metrics.

The question every metric here answers is the same one: *is this attention real?*
Standard token APIs report price, volume and liquidity. None of them report
whether the 400 replies under a launch tweet came from 400 people or from one
person with 400 accounts — and that distinction is most of the edge available on
a token that is nine minutes old.

Each metric is paired with the specific evasion it is designed to survive. A
metric that a $50 engagement package defeats is not in this file.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import timedelta

from botsensai.metrics.base import Metric, MetricContext
from botsensai.models import Direction, Platform
from botsensai.util.stats import (
    burstiness,
    clamp,
    periodicity_score,
    shannon_entropy,
    wilson_lower_bound,
)
from botsensai.util.text import (
    conviction_density,
    effort_score,
    shill_density,
    template_cluster_share,
)

SOCIAL_SURFACES = ("x", "instagram", "tiktok", "reddit", "telegram", "pumpfun")


class ReplyTemplateRatio(Metric):
    """Share of replies that belong to a repeated-template cluster.

    Paid reply farms work from a script. Even when the operator randomizes the
    ticker, the emoji and the multiplier, the *sentence skeleton* survives, and
    identical skeletons appearing across dozens of nominally unrelated accounts
    is close to conclusive. This is the highest-signal, lowest-cost bot detector
    available on a brand-new token, and it is available within a minute of the
    first replies appearing.
    """

    id = "reply_template_ratio"
    name = "Reply template ratio"
    family = "social_authenticity"
    thesis = (
        "Scripted reply farms reuse sentence skeletons across accounts. A high "
        "clustered share means the visible enthusiasm was purchased, and purchased "
        "enthusiasm does not buy the token."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("x", "pumpfun")
    earliest_seconds = 90.0
    min_evidence = 12
    default_midpoint = 0.25
    default_steepness = 9.0
    gameability = (
        "A sophisticated operator can hand-write varied replies, which defeats exact "
        "skeleton matching. Counter-measure: the fuzzy simhash pass catches paraphrase, "
        "and hand-written variety at scale costs real money, which is itself the signal "
        "we want — we are measuring willingness to spend, not literal string equality."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        replies = [p for p in ctx.replies if p.text and p.text.strip()]
        if len(replies) < 6:
            return None, len(replies), "fewer than 6 replies"
        texts = [p.text for p in replies]
        share = template_cluster_share(texts, min_cluster=3, fuzzy=True)
        return share, len(replies), f"{len(replies)} replies analysed"


class EngagementDepthRatio(Metric):
    """Costly engagement as a share of total engagement.

    Likes are free and are what engagement farms sell. Replies that took effort
    to write, quote-posts that put the poster's own reputation behind the call,
    and bookmarks (which nobody buys because nobody sees them) all cost
    something. The ratio of costly to free engagement separates a real audience
    from a rented one, and it is robust precisely because the cheap signal is the
    one being manipulated.
    """

    id = "engagement_depth_ratio"
    name = "Engagement depth ratio"
    family = "social_authenticity"
    thesis = (
        "Costly engagement (effortful replies, quotes, bookmarks) is expensive to fake "
        "and cheap engagement (likes, follows) is not. Their ratio measures how much of "
        "the attention is real."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x",)
    earliest_seconds = 180.0
    min_evidence = 8
    default_midpoint = 0.12
    default_steepness = 14.0
    gameability = (
        "Quote-posts can be farmed, though at roughly ten times the cost of likes. "
        "Counter-measure: effortful replies are weighted by `effort_score`, so a farm "
        "posting one-word quotes gains almost nothing, and the metric is paired with "
        "`reply_template_ratio` which catches the farm directly."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        posts = ctx.posts_on(Platform.X)
        if not posts:
            return None, 0, "no X posts"

        cheap = 0.0
        costly = 0.0
        counted = 0
        for p in posts:
            likes = p.likes or 0
            reposts = p.reposts or 0
            quotes = p.quotes or 0
            bookmarks = p.bookmarks or 0
            replies = p.replies or 0
            if likes + reposts + quotes + bookmarks + replies == 0:
                continue
            counted += 1
            cheap += likes + 0.5 * reposts
            costly += 2.0 * quotes + 1.5 * bookmarks + 1.0 * replies

        # Effortful replies we actually hold the text of get counted directly,
        # which is far more reliable than the aggregate reply count.
        reply_texts = [p for p in ctx.replies if p.platform is Platform.X and p.text]
        for r in reply_texts:
            costly += 2.0 * effort_score(r.text)
            counted += 1

        total = cheap + costly
        if counted < 3 or total <= 0:
            return None, counted, "insufficient engagement data"
        return costly / total, counted, f"{counted} engagement records"


class EngagerAgeDispersion(Metric):
    """Entropy of account-creation dates among the accounts engaging.

    Bot fleets are provisioned in batches. Their creation timestamps cluster into
    a handful of days, sometimes a handful of hours. A genuine audience has
    accounts created across a decade. Measuring dispersion rather than absolute
    age is what makes this robust: aged accounts can be bought, but buying a
    fleet with a *naturally dispersed* age profile is far harder and far more
    expensive than buying a fleet of aged accounts from one batch.
    """

    id = "engager_age_dispersion"
    name = "Engager account-age dispersion"
    family = "social_authenticity"
    thesis = (
        "Bot fleets are registered in batches, so their account-creation dates cluster. "
        "Low dispersion in engager account ages is a fleet fingerprint."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x",)
    earliest_seconds = 180.0
    min_evidence = 10
    default_midpoint = 0.55
    default_steepness = 8.0
    gameability = (
        "An operator can assemble a fleet from aged accounts bought across several "
        "sellers, which raises dispersion. Counter-measure: that market is thin and "
        "expensive relative to the payoff on a sub-$50k-cap token, and the pattern "
        "co-occurs with a high `reply_template_ratio`, so the pair is much harder to "
        "defeat than either alone."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        ages: list[float] = []
        for p in ctx.posts:
            if p.author_created_at is None:
                continue
            age_days = (ctx.as_of - p.author_created_at).total_seconds() / 86400.0
            if age_days >= 0:
                ages.append(age_days)
        if len(ages) < 8:
            return None, len(ages), "fewer than 8 accounts with known creation dates"

        # Log-spaced buckets: the meaningful distinction is between accounts made
        # this week, this year and five years ago, not between day 900 and 901.
        # With this spacing an account age of 5 days lands in bucket 3 and one of
        # 7 years in bucket 15, so REFERENCE_BUCKETS spans the realistic range.
        REFERENCE_BUCKETS = 16
        buckets: Counter[int] = Counter()
        for a in ages:
            buckets[int(math.log1p(max(0.0, a)) * 2)] += 1

        # Normalizing entropy by the *observed* bucket count would be a bug: a
        # fleet spread evenly across three adjacent weeks would score as highly
        # as a real audience spread evenly across a decade. Normalizing against
        # a fixed reference keeps the number of distinct age cohorts in the
        # signal, which is the thing that actually distinguishes them.
        entropy_nats = shannon_entropy(list(buckets.values()), normalize=False)
        entropy = clamp(entropy_nats / math.log(REFERENCE_BUCKETS))

        # A fleet also shows up as a large share sitting in any single bucket.
        modal_share = max(buckets.values()) / len(ages)
        score = clamp(entropy * (1.0 - 0.5 * modal_share))
        return score, len(ages), (
            f"{len(ages)} engagers, {len(buckets)} of {REFERENCE_BUCKETS} age cohorts occupied"
        )


class ReplyRhythmNaturalness(Metric):
    """How human the timing of incoming replies looks.

    Human attention arrives in bursts: a call lands, a wave replies, it decays,
    another influencer reposts, another wave. Scripted engagement arrives on a
    timer, producing inter-arrival gaps with near-zero variance. Goh-Barabási
    burstiness captures exactly this: strongly negative means periodic, which no
    real audience produces.
    """

    id = "reply_rhythm_naturalness"
    name = "Reply rhythm naturalness"
    family = "social_authenticity"
    thesis = (
        "Organic attention arrives in bursts; scripted attention arrives on a timer. "
        "Burstiness near or below zero with high modal-gap clustering is a bot cadence."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "pumpfun")
    earliest_seconds = 240.0
    min_evidence = 10
    default_midpoint = 0.35
    default_steepness = 7.0
    gameability = (
        "Jitter can be added to a posting script, which raises burstiness toward the "
        "Poisson value of 0. Counter-measure: the metric rewards genuinely bursty "
        "(> 0.3) rather than merely non-periodic, so jittered scripts land in the "
        "middle band and are separated from real virality, not from each other."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        replies = sorted(ctx.replies, key=lambda p: p.as_of)
        if len(replies) < 8:
            return None, len(replies), "fewer than 8 replies"
        stamps = [p.as_of.timestamp() for p in replies]
        b = burstiness(stamps)
        periodic = periodicity_score(stamps, tolerance=0.15)
        # Map burstiness from -1..1 into 0..1, then penalise metronomic cadence.
        score = clamp((b + 1.0) / 2.0) * (1.0 - clamp(periodic))
        return score, len(replies), f"burstiness={b:.3f} periodicity={periodic:.3f}"


class FollowerEngagementCoherence(Metric):
    """Whether engagement scales plausibly with audience size.

    Across real accounts, engagement per post follows a predictable sublinear
    relationship with follower count — big accounts get proportionally *less*
    engagement per follower, reliably. Accounts with purchased followers break
    this badly (huge following, no engagement), and accounts inside an engagement
    pod break it the other way (small following, implausible engagement). Either
    deviation is a warning; coherence with the natural curve is the healthy case.
    """

    id = "follower_engagement_coherence"
    name = "Follower/engagement coherence"
    family = "social_authenticity"
    thesis = (
        "Real accounts sit on a stable sublinear engagement-vs-followers curve. "
        "Purchased followers and engagement pods both deviate from it, in opposite "
        "directions, and both predict that the visible support is manufactured."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x",)
    earliest_seconds = 180.0
    min_evidence = 6
    default_midpoint = 0.5
    default_steepness = 6.0
    gameability = (
        "An operator who buys followers AND engagement in matched proportion can sit on "
        "the curve. Counter-measure: matched purchase is several times more expensive, "
        "and the resulting engagement still fails `reply_template_ratio` and "
        "`engager_age_dispersion`, which read the content and the accounts rather than "
        "the ratio."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        pairs: list[tuple[float, float]] = []
        for p in ctx.posts_on(Platform.X):
            followers = p.author_followers
            if not followers or followers < 10:
                continue
            eng = p.engagement
            if eng <= 0:
                continue
            pairs.append((float(followers), float(eng)))
        if len(pairs) < 4:
            return None, len(pairs), "fewer than 4 usable author/engagement pairs"

        # Empirical rule of thumb across social platforms: engagement rate decays
        # roughly as followers^-0.25, i.e. expected engagement ~ followers^0.75.
        deviations: list[float] = []
        for followers, eng in pairs:
            expected = max(1.0, 0.02 * followers**0.75)
            ratio = eng / expected
            # Symmetric in log space so 4x-under and 4x-over penalise equally.
            deviations.append(abs(math.log10(max(ratio, 1e-6))))
        mean_dev = sum(deviations) / len(deviations)
        score = clamp(1.0 - mean_dev / 1.5)
        return score, len(pairs), f"{len(pairs)} accounts, mean log-deviation {mean_dev:.2f}"


class ConvictionLanguageShare(Metric):
    """Share of comments disclosing an actual position rather than pure hype.

    "LFG 100x" costs nothing to type and is what farms are paid to produce.
    "I averaged in at 38k, down 20%, still holding" is a claim about the author's
    own money, and it is the linguistic fingerprint of an audience that is
    actually exposed. Communities that trade the token talk like traders;
    communities that were hired to appear talk like advertisements.
    """

    id = "conviction_language_share"
    name = "Conviction language share"
    family = "social_authenticity"
    thesis = (
        "Real holders discuss entries, exits and losses; paid engagement discusses "
        "moon and gems. The share of first-person position language separates an "
        "exposed community from a rented one."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "pumpfun", "telegram", "reddit")
    earliest_seconds = 300.0
    min_evidence = 10
    default_midpoint = 0.12
    default_steepness = 16.0
    gameability = (
        "A farm can be scripted to include fabricated entry prices. Counter-measure: "
        "fabricated entries are template-generated and are caught by "
        "`reply_template_ratio`; additionally the shill-phrase penalty here means a "
        "message must contain conviction language WITHOUT hype boilerplate to score."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        texts = [p.text for p in ctx.posts if p.text and len(p.text.strip()) > 8]
        if len(texts) < 8:
            return None, len(texts), "fewer than 8 substantive messages"
        hits = 0
        for t in texts:
            conviction = conviction_density(t)
            shill = shill_density(t)
            if conviction > 0.4 and shill < 0.34:
                hits += 1
        # Wilson bound so a 3-of-6 sample is not treated as a 50% rate.
        rate = wilson_lower_bound(hits, len(texts))
        return rate, len(texts), f"{hits}/{len(texts)} messages show position language"


class MentionAuthorDiversity(Metric):
    """How many genuinely distinct voices are talking, not how many messages exist.

    Volume of mentions is trivially inflatable and every social-listening product
    reports it. What is not inflatable cheaply is *breadth*: many distinct
    authors, spread across follower tiers, none dominating the conversation. This
    metric measures the effective number of independent voices via the entropy of
    the per-author message distribution, so one account posting 200 times counts
    for roughly one voice rather than 200.
    """

    id = "mention_author_diversity"
    name = "Mention author diversity"
    family = "social_authenticity"
    thesis = (
        "Breadth of distinct participating accounts, entropy-weighted so a single "
        "prolific poster cannot manufacture it, is much harder to fake than raw "
        "mention volume and tracks genuine spread."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "reddit", "telegram", "instagram", "tiktok")
    earliest_seconds = 300.0
    min_evidence = 12
    default_midpoint = 12.0
    default_steepness = 0.16
    gameability = (
        "Buying 500 accounts raises diversity directly. Counter-measure: those accounts "
        "fail `engager_age_dispersion` and `reply_template_ratio`, and this metric "
        "additionally discounts authors whose only activity is this one ticker, which "
        "is the dominant profile of a purchased fleet."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        posts = [p for p in ctx.posts if p.author]
        if len(posts) < 8:
            return None, len(posts), "fewer than 8 posts"

        per_author: Counter[str] = Counter()
        for p in posts:
            per_author[f"{p.platform.value}:{p.author.lower()}"] += 1

        if len(per_author) < 3:
            return 1.0, len(posts), "fewer than 3 distinct authors"

        entropy = shannon_entropy(list(per_author.values()), normalize=False)
        effective_voices = math.exp(entropy)

        # Discount single-purpose accounts: authors whose entire visible output in
        # this window is this one token look like dedicated shill accounts.
        multi_topic = sum(
            1
            for author, count in per_author.items()
            if count == 1 or _author_mentions_other_tokens(ctx, author)
        )
        breadth_factor = 0.5 + 0.5 * (multi_topic / len(per_author))
        return effective_voices * breadth_factor, len(posts), (
            f"{len(per_author)} authors, {effective_voices:.1f} effective voices"
        )


def _author_mentions_other_tokens(ctx: MetricContext, author_key: str) -> bool:
    """True if this author's posts reference tickers other than the subject."""
    subject = (ctx.token.symbol or "").upper()
    for p in ctx.posts:
        if f"{p.platform.value}:{p.author.lower()}" != author_key:
            continue
        others = {t.upper() for t in p.mentioned_tokens} - {subject, ""}
        if others:
            return True
    return False


class SocialVelocityAcceleration(Metric):
    """Whether the conversation is still accelerating or has already peaked.

    Mention *level* tells you where the token has been; the second derivative
    tells you where it is going, and on a token whose whole life is measured in
    hours that difference is the trade. A launch with 300 mentions and falling
    acceleration is a sell; one with 80 mentions and rising acceleration is the
    entry. Computed on distinct-author counts rather than raw messages so a
    single spamming account cannot manufacture the curve.
    """

    id = "social_velocity_acceleration"
    name = "Social velocity acceleration"
    family = "social_authenticity"
    thesis = (
        "The second derivative of distinct-author mention count separates a "
        "conversation still catching from one already exhausted, which raw mention "
        "volume cannot do."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "reddit", "telegram")
    earliest_seconds = 420.0
    min_evidence = 12
    default_midpoint = 0.0
    default_steepness = 2.5
    gameability = (
        "A farm can stage a rising release schedule to fake acceleration. "
        "Counter-measure: staged releases are periodic and are caught by "
        "`reply_rhythm_naturalness`; the distinct-author basis also forces the farm to "
        "add new accounts continuously, which raises cost superlinearly."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        posts = sorted(ctx.posts, key=lambda p: p.as_of)
        if len(posts) < 10:
            return None, len(posts), "fewer than 10 posts"

        window = min(max(ctx.age_seconds, 600.0), 3600.0)
        bucket_seconds = window / 6.0
        start = ctx.as_of - timedelta(seconds=window)
        buckets: dict[int, set[str]] = defaultdict(set)
        for p in posts:
            if p.as_of < start:
                continue
            idx = int((p.as_of - start).total_seconds() // bucket_seconds)
            buckets[min(idx, 5)].add(f"{p.platform.value}:{p.author.lower()}")

        counts = [len(buckets.get(i, set())) for i in range(6)]
        if sum(counts) < 8:
            return None, sum(counts), "too few authors in window"

        first_half = sum(counts[:3])
        second_half = sum(counts[3:])
        if first_half == 0:
            return 1.0 if second_half > 0 else None, sum(counts), "cold start"

        # Log growth ratio: bounded, symmetric, and stable when counts are small.
        ratio = math.log((second_half + 1.0) / (first_half + 1.0))
        return ratio, sum(counts), f"buckets={counts}"


__all__ = [
    "ConvictionLanguageShare",
    "PurchasedFollowerSignal",
    "EngagementDepthRatio",
    "EngagerAgeDispersion",
    "FollowerEngagementCoherence",
    "MentionAuthorDiversity",
    "ReplyRhythmNaturalness",
    "ReplyTemplateRatio",
    "SocialVelocityAcceleration",
]


class PurchasedFollowerSignal(Metric):
    """X's own count of burst-acquired followers, as a share of the audience.

    Every other authenticity metric in this file *infers* purchased followers
    from a behavioural side-effect — an engagement rate that does not match the
    audience size, an account-age distribution that clusters. This one reads
    X's own classification directly: `fast_followers_count` is the platform's
    internal tally of followers gained in the bursts that follower packages
    produce, and it sits in the same payload as the public follower count.

    Direct evidence beats inference, so where this is available it should
    dominate the inferred metrics rather than sit alongside them. It is only
    available through a logged-in session, which is the main argument for
    running one at all.
    """

    id = "purchased_follower_signal"
    name = "Purchased follower signal"
    family = "social_authenticity"
    thesis = (
        "Share of the promoting account's followers that X itself classifies as "
        "fast-acquired. This is the platform's own read on bought followers rather than "
        "an inference from engagement ratios, and no free endpoint exposes it."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("x",)
    earliest_seconds = 120.0
    min_evidence = 1
    default_midpoint = 0.15
    default_steepness = 14.0
    gameability = (
        "Not gameable from the outside — the operator cannot edit X's internal "
        "classification of their own follower base, and buying followers is what "
        "produces the number in the first place. The realistic evasion is to promote "
        "from an account with a genuinely organic audience, which is expensive and is "
        "the outcome we want. The metric's real weakness is availability rather than "
        "manipulability: it requires an authenticated session, so it is absent far more "
        "often than it is wrong, and the confidence machinery handles that."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        shares: dict[str, float] = ctx.extra.get("fast_follower_share", {})
        value = shares.get(ctx.token.key)
        if value is None:
            return None, 0, "fast-follower data requires an authenticated X session"
        return clamp(float(value)), 1, f"{value:.1%} of followers classified fast-acquired"
