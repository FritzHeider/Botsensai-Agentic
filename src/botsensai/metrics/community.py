"""Community content-production metrics.

Engagement can be bought. *Labour* is much harder to buy. When strangers start
making their own memes, editing their own videos and arguing about the token in
places the team does not control, something has happened that a marketing budget
cannot reliably manufacture at the sizes these tokens operate at.

These metrics measure production rather than reaction, which is the distinction
no standard API exposes.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from botsensai.media.phash import cluster_by_distance, perceptual_values
from botsensai.metrics.base import Metric, MetricContext
from botsensai.models import Direction, Platform, SocialPost
from botsensai.util.stats import clamp, saturating, shannon_entropy
from botsensai.util.text import effort_score, shill_density, similarity

TEAM_PLATFORMS = (Platform.X, Platform.TELEGRAM)
ORGANIC_PLATFORMS = (
    Platform.INSTAGRAM,
    Platform.TIKTOK,
    Platform.REDDIT,
    Platform.YOUTUBE,
    Platform.FOURCHAN,
)


def _author_key(post: SocialPost) -> str:
    return f"{post.platform.value}:{post.author.lower()}"


def _team_accounts(ctx: MetricContext) -> set[str]:
    """Accounts we can attribute to the team, so their output is excluded.

    Anything the team produces is marketing, not community. Getting this
    exclusion right is what makes the rest of the file meaningful.
    """
    team: set[str] = set()
    if ctx.launch is None:
        return team
    handle = (ctx.launch.twitter or "").rstrip("/").split("/")[-1].lower().lstrip("@")
    if handle:
        team.add(f"{Platform.X.value}:{handle}")
    tg = (ctx.launch.telegram or "").rstrip("/").split("/")[-1].lower()
    if tg:
        team.add(f"{Platform.TELEGRAM.value}:{tg}")
    return team


class OrganicMediaProductionRate(Metric):
    """Distinct non-team accounts producing original media, per hour.

    This is the single most expensive thing on this list to fake. Someone has to
    open an editor, make a picture or a video, and post it under their own name.
    Engagement farms sell likes and replies; they do not sell original creative
    work, because at the price point of a memecoin campaign the labour does not
    pencil. When this number is meaningfully above zero in the first hour, the
    token has crossed from promotion into culture, which is the only thing that
    has ever sustained one of these past a day.
    """

    id = "organic_media_production_rate"
    name = "Organic media production rate"
    family = "community_production"
    thesis = (
        "Distinct non-team accounts posting original images or video per hour. "
        "Creative labour is the least purchasable signal available and marks the "
        "transition from paid promotion to genuine participation."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "instagram", "tiktok", "reddit")
    earliest_seconds = 600.0
    min_evidence = 3
    default_midpoint = 4.0
    default_steepness = 0.4
    gameability = (
        "A team can commission memes and post them from sockpuppets. Counter-measure: "
        "those accounts cluster in creation date (`engager_age_dispersion`) and the "
        "media reuses a small set of source images, which `derivative_remix_depth` "
        "detects as low perceptual-hash diversity. Commissioning genuinely varied "
        "creative from varied accounts costs more than the tokens at this cap are worth."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.age_seconds < 60.0:
            return None, 0, "token too young"
        team = _team_accounts(ctx)
        creators: set[str] = set()
        media_posts = 0
        for p in ctx.posts:
            if not p.media_urls and not p.media_hashes:
                continue
            if p.is_repost or p.parent_id is not None:
                continue
            key = _author_key(p)
            if key in team:
                continue
            # A pure advertisement with an image is not community production.
            if p.text and shill_density(p.text) > 0.66 and effort_score(p.text) < 0.2:
                continue
            creators.add(key)
            media_posts += 1

        if media_posts == 0:
            return 0.0, max(1, len(ctx.posts) // 10), "no original media found"

        hours = max(0.25, ctx.age_seconds / 3600.0)
        rate = len(creators) / hours
        return rate, media_posts, f"{len(creators)} creators, {media_posts} media posts"


class DerivativeRemixDepth(Metric):
    """How far the token's imagery has drifted from the original asset.

    A token launches with one picture. If nothing happens, every image anyone
    posts is that same picture. If a community forms, the picture gets recut,
    recaptioned, redrawn, put in new contexts — and the population of perceptual
    hashes fans out. Measuring the *diversity* of derived media rather than its
    volume is what makes this hard to fake: reposting the original a thousand
    times moves this metric not at all.
    """

    id = "derivative_remix_depth"
    name = "Derivative remix depth"
    family = "community_production"
    thesis = (
        "Entropy of perceptual-hash clusters across posted media. A wide, evenly "
        "populated cluster set means people are making new things from the original, "
        "which is the observable signature of a meme becoming self-sustaining."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "instagram", "tiktok", "reddit")
    earliest_seconds = 900.0
    min_evidence = 6
    default_midpoint = 0.35
    default_steepness = 7.0
    gameability = (
        "A team can generate variants with an image model and post them. "
        "Counter-measure: model-generated variant sets cluster tightly in hash space "
        "relative to human remixes and arrive in a burst, so they lift cluster count "
        "but not the evenness term, and they trip `reply_rhythm_naturalness`."
    )

    #: Bits of a 64-bit perceptual hash two images may differ by and still count
    #: as the same visual idea. A re-encode or a resize moves 0-4 bits; a
    #: recaption or a crop moves 8-16; an unrelated picture sits near 32, which
    #: is where random 64-bit strings land. 10 separates re-posting from remixing.
    cluster_distance = 10

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        hashes: list[str] = []
        for p in ctx.posts:
            hashes.extend(h for h in p.media_hashes if h)
        # Only perceptual hashes may be compared to each other. 4chan hands us
        # an MD5, which answers "same file" and is stored, but two MD5s of the
        # same picture re-encoded are as far apart as two of different pictures,
        # so counting them as visual clusters would read every re-upload as a
        # remix — the exact artefact this metric exists to avoid.
        digests = perceptual_values(hashes)
        if len(digests) < 5:
            return None, len(digests), "fewer than 5 perceptually hashed media items"

        # Single-linkage clustering in Hamming space. Prefix bucketing cannot do
        # this job: perceptual hashes of near-identical images differ by a few
        # bits in arbitrary positions, so any two of them almost never share a
        # prefix and every image would read as its own cluster.
        clusters: Counter[int] = Counter(cluster_by_distance(digests, self.cluster_distance))
        if len(clusters) < 2:
            return 0.0, len(digests), "all media perceptually identical"

        evenness = shannon_entropy(list(clusters.values()), normalize=True)
        breadth = saturating(float(len(clusters)), scale=8.0)
        # Both terms required: many clusters dominated by one, or two even
        # clusters, are each unimpressive. Their product is the real signal.
        return evenness * breadth, len(digests), (
            f"{len(clusters)} visual clusters over {len(digests)} items"
        )


class CrossPlatformPropagationLag(Metric):
    """How fast the token escaped the platform it was launched on.

    Crypto Twitter talking about a Solana token proves nothing; that is the
    room it was launched into. The moment it appears on TikTok, Instagram or a
    non-crypto subreddit, it has reached an audience that was not targeted, and
    that jump is the closest thing to a leading indicator of retail inflow that
    exists on these timescales. Short lag is strongly bullish; a token still
    confined to X after two hours has not spread, it has been advertised.
    """

    id = "cross_platform_propagation_lag"
    name = "Cross-platform propagation lag"
    family = "community_production"
    thesis = (
        "Elapsed time from the first crypto-native mention to the first mention on a "
        "platform the team did not target. Escaping the launch venue is the earliest "
        "observable signal of genuine reach."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "instagram", "tiktok", "reddit", "youtube")
    earliest_seconds = 900.0
    min_evidence = 2
    default_midpoint = 0.0
    default_steepness = 1.4
    gameability = (
        "A team can pay for a TikTok and an IG post at launch. Counter-measure: the "
        "metric requires distinct authors on the second platform and discounts accounts "
        "whose posting history is exclusively promotional, so a single bought post "
        "produces almost no lift; genuine propagation shows several unrelated authors."
    )

    @staticmethod
    def _origin_timestamp(ctx: MetricContext) -> float:
        """When the token first showed up on a venue the team controls."""
        for p in sorted(ctx.posts, key=lambda x: x.as_of):
            if p.platform in TEAM_PLATFORMS:
                return p.as_of.timestamp()
        assert ctx.launch is not None
        return ctx.launch.created_at.timestamp()

    @staticmethod
    def _organic_spread(ctx: MetricContext, team: set[str]) -> tuple[dict[Platform, set[str]], float | None]:
        """Distinct non-team authors per off-platform venue, and the earliest such post."""
        organic_authors: dict[Platform, set[str]] = defaultdict(set)
        organic_first: float | None = None
        for p in sorted(ctx.posts, key=lambda x: x.as_of):
            if p.platform not in ORGANIC_PLATFORMS:
                continue
            key = _author_key(p)
            if key in team:
                continue
            if p.text and shill_density(p.text) > 0.66:
                continue
            organic_authors[p.platform].add(key)
            if organic_first is None:
                organic_first = p.as_of.timestamp()
        return organic_authors, organic_first

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.launch is None:
            return None, 0, "no launch record"

        origin_first = self._origin_timestamp(ctx)
        organic_authors, organic_first = self._organic_spread(ctx, _team_accounts(ctx))
        distinct_authors = sum(len(v) for v in organic_authors.values())
        if organic_first is None or distinct_authors < 2:
            # Not yet escaped. Bearish, but honestly so: report a large lag
            # rather than nothing, because "still contained" is information.
            elapsed_hours = ctx.age_seconds / 3600.0
            if elapsed_hours < 0.25:
                return None, distinct_authors, "too early to judge propagation"
            return -math.log1p(elapsed_hours), max(1, distinct_authors), (
                "no independent off-platform mentions yet"
            )

        lag_hours = max(0.0, (organic_first - origin_first) / 3600.0)
        platform_bonus = 0.35 * (len(organic_authors) - 1)
        score = -math.log1p(lag_hours) + platform_bonus
        return score, distinct_authors, (
            f"lag {lag_hours:.2f}h across {len(organic_authors)} platforms, "
            f"{distinct_authors} independent authors"
        )


class UnpaidPromoterShare(Metric):
    """Share of promoting accounts that do not look professionally promotional.

    Every ticker gets shilled by the same rotating cast of accounts whose entire
    timeline is one call after another. Those accounts are paid, their audience
    knows they are paid, and their involvement carries close to zero information
    about the token. What matters is the share of talkers who are *not* in that
    business — people who normally post about other things and made an exception
    for this. That ratio is the difference between a promotion and a phenomenon.
    """

    id = "unpaid_promoter_share"
    name = "Unpaid promoter share"
    family = "community_production"
    thesis = (
        "Fraction of mentioning accounts whose behaviour does not match the "
        "professional-caller profile. Organic advocates carry information; paid "
        "callers carry almost none, and standard APIs do not distinguish them."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "reddit", "telegram")
    earliest_seconds = 600.0
    min_evidence = 8
    default_midpoint = 0.55
    default_steepness = 7.0
    gameability = (
        "Callers can dilute their timelines with non-crypto filler to look organic. "
        "Counter-measure: the profile test uses ticker-mention breadth per unit time, "
        "which filler does not change — an account calling nine different tokens today "
        "is a caller regardless of what else it posts."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        team = _team_accounts(ctx)
        subject = (ctx.token.symbol or "").upper()

        per_author_tokens: dict[str, set[str]] = defaultdict(set)
        per_author_posts: dict[str, list[SocialPost]] = defaultdict(list)
        for p in ctx.posts:
            key = _author_key(p)
            if key in team:
                continue
            per_author_posts[key].append(p)
            per_author_tokens[key].update(t.upper() for t in p.mentioned_tokens if t)

        authors = list(per_author_posts.keys())
        if len(authors) < 6:
            return None, len(authors), "fewer than 6 distinct authors"

        organic = 0
        for key in authors:
            other_tickers = per_author_tokens[key] - {subject, ""}
            posts = per_author_posts[key]
            avg_shill = sum(shill_density(p.text or "") for p in posts) / max(1, len(posts))
            avg_effort = sum(effort_score(p.text or "") for p in posts) / max(1, len(posts))

            # A professional caller: many distinct tickers in a short window, high
            # promotional density, low effort per message.
            looks_professional = (
                len(other_tickers) >= 4 or (avg_shill > 0.6 and avg_effort < 0.25)
            )
            if not looks_professional:
                organic += 1

        share = organic / len(authors)
        return share, len(authors), f"{organic}/{len(authors)} authors look organic"


class CommunityContentOriginality(Metric):
    """How much of what the community writes is its own material.

    Beyond images, a live community develops its own vocabulary: in-jokes,
    nicknames, recurring bits that were not in the launch copy. A dead one
    recycles the launch copy verbatim. Measuring text distance from the token's
    own description and from the highest-engagement post catches this directly,
    and it works on tokens that never produce a single image.
    """

    id = "community_content_originality"
    name = "Community content originality"
    family = "community_production"
    thesis = (
        "Average textual distance between community messages and the team's own launch "
        "copy. A community writing its own material has formed; one echoing the pitch "
        "has not."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("x", "pumpfun", "telegram", "reddit")
    earliest_seconds = 600.0
    min_evidence = 10
    default_midpoint = 0.62
    default_steepness = 8.0
    gameability = (
        "A farm can be scripted with varied copy that differs from the launch text. "
        "Counter-measure: that varied copy is self-similar even when it differs from "
        "the source, which `reply_template_ratio` measures directly; this metric is "
        "only trusted when that one is clean, via the scorer's family weighting."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        team = _team_accounts(ctx)
        reference_parts: list[str] = []
        if ctx.launch is not None:
            if ctx.launch.description:
                reference_parts.append(ctx.launch.description)
            if ctx.launch.token.name:
                reference_parts.append(ctx.launch.token.name)
        if not reference_parts:
            return None, 0, "no launch copy to compare against"
        reference = " ".join(reference_parts)

        community_texts = [
            p.text
            for p in ctx.posts
            if p.text and len(p.text.strip()) > 15 and _author_key(p) not in team
        ]
        if len(community_texts) < 8:
            return None, len(community_texts), "fewer than 8 substantive community messages"

        distances = [1.0 - similarity(t, reference) for t in community_texts]
        mean_distance = sum(distances) / len(distances)

        # Weight by effort: originality expressed in one-word posts is noise.
        mean_effort = sum(effort_score(t) for t in community_texts) / len(community_texts)
        score = clamp(mean_distance * (0.5 + 0.5 * min(1.0, mean_effort * 2.0)))
        return score, len(community_texts), (
            f"{len(community_texts)} messages, mean distance {mean_distance:.2f}, "
            f"mean effort {mean_effort:.2f}"
        )


__all__ = [
    "CommunityContentOriginality",
    "CrossPlatformPropagationLag",
    "DerivativeRemixDepth",
    "OrganicMediaProductionRate",
    "UnpaidPromoterShare",
]
