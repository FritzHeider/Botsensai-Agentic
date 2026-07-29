"""Narrative metrics.

Memecoins are not valued on cash flows, they are valued on whether an idea
spreads. That makes the idea itself measurable input, and it is input no market
data API contains: whether the concept is new or the fourth copy of this week's
winner, whether it fits the meta the market is currently rewarding, and whether
its name is so contested that nobody can find it.
"""

from __future__ import annotations

from botsensai.metrics.base import Metric, MetricContext
from botsensai.models import Direction
from botsensai.util.stats import clamp, saturating
from botsensai.util.text import novelty_vs_corpus, similarity, tokens


def _token_narrative(ctx: MetricContext) -> str:
    parts: list[str] = []
    if ctx.token.name:
        parts.append(ctx.token.name)
    if ctx.token.symbol:
        parts.append(ctx.token.symbol)
    if ctx.launch is not None and ctx.launch.description:
        parts.append(ctx.launch.description)
    return " ".join(parts).strip()


class NarrativeNovelty(Metric):
    """Distance from everything else that launched recently.

    The copy problem is the defining failure mode of this asset class. A winner
    appears and within an hour there are thirty derivatives, of which zero
    succeed, because the attention the original captured is precisely the thing
    the copies cannot have. Measuring textual and conceptual distance from the
    recent launch corpus separates the original from the swarm chasing it — and
    it has to be measured against the *recent* corpus specifically, because
    novelty is relative to what the market has just seen, not to all history.
    """

    id = "narrative_novelty"
    name = "Narrative novelty"
    family = "narrative"
    thesis = (
        "Distance between this token's name and description and the corpus of tokens "
        "launched in the last several hours. Derivatives of a current winner reliably "
        "fail; the original reliably captured the attention they are chasing."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("pumpfun", "moonshot", "apestore", "believe")
    earliest_seconds = 0.0
    min_evidence = 20
    default_midpoint = 0.72
    default_steepness = 9.0
    gameability = (
        "A copycat can rename itself to score as novel while keeping the winning "
        "imagery. Counter-measure: novelty is measured on name, symbol and description "
        "jointly, and the imagery path is covered by `derivative_remix_depth`, which "
        "sees perceptual hashes rather than text."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        narrative = _token_narrative(ctx)
        if len(narrative) < 4:
            return None, 0, "no name or description"
        corpus = [c for c in ctx.recent_narratives if c and c.strip()]
        if len(corpus) < 15:
            return None, len(corpus), "recent-launch corpus too small to judge novelty"

        novelty = novelty_vs_corpus(narrative, corpus, k=4)

        # A ticker that is a near-exact match of several recent launches is worse
        # than one that resembles a single launch: it is in a copy swarm.
        near_copies = sum(1 for c in corpus if similarity(narrative, c, k=4) > 0.55)
        swarm_penalty = clamp(near_copies / 8.0)
        score = clamp(novelty * (1.0 - 0.6 * swarm_penalty))
        return score, len(corpus), (
            f"novelty {novelty:.2f} vs {len(corpus)} recent launches, {near_copies} near-copies"
        )


class MetaAlignment(Metric):
    """Fit with the narrative theme the market is currently paying for.

    Novelty alone is not enough; a genuinely original idea nobody wants is still
    worthless. At any moment a handful of themes are being rewarded, and that set
    turns over in days. This metric reads the currently-winning themes out of the
    system's own memory — the regime notes the bot has written from its recent
    observations — and measures the token against them. It is the point where the
    agentic memory layer feeds directly into a trade decision rather than sitting
    beside it.
    """

    id = "meta_alignment"
    name = "Meta alignment"
    family = "narrative"
    thesis = (
        "Overlap between this token's narrative and the themes that have recently "
        "produced winners, read from the agent's own regime memory. Novelty pays only "
        "when it points at something the market currently wants."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("memory",)
    earliest_seconds = 0.0
    min_evidence = 3
    default_midpoint = 0.2
    default_steepness = 9.0
    gameability = (
        "A team can stuff its description with whatever is trending. Counter-measure: "
        "keyword stuffing collapses `narrative_novelty` in the same pass, and the "
        "scorer requires both, so a token cannot win by being a trend-matched clone."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        themes: list[tuple[str, float]] = ctx.extra.get("active_themes", [])
        if not themes:
            return None, 0, "no active theme memory"

        narrative = _token_narrative(ctx)
        if len(narrative) < 4:
            return None, 0, "no name or description"
        narrative_tokens = set(tokens(narrative))
        if not narrative_tokens:
            return None, 0, "narrative has no content words"

        best = 0.0
        matched = ""
        for theme, weight in themes:
            theme_tokens = set(tokens(theme))
            if not theme_tokens:
                continue
            overlap = len(narrative_tokens & theme_tokens) / len(theme_tokens)
            textual = similarity(narrative, theme, k=4)
            combined = max(overlap, textual) * clamp(weight)
            if combined > best:
                best = combined
                matched = theme
        return best, len(themes), (
            f"best theme match {best:.2f}" + (f" ({matched[:40]})" if matched else "")
        )


class TickerContention(Metric):
    """How crowded the token's name is.

    A ticker shared with eleven live tokens is a discovery problem: every search,
    every chart link, every conversation splits across impostors, and the honest
    launch loses attention to whichever copy is pumping. This is trivially
    observable from the launch feed and completely absent from per-token APIs,
    which only ever show you the token you asked about.
    """

    id = "ticker_contention"
    name = "Ticker contention"
    family = "narrative"
    thesis = (
        "Count of other live tokens sharing this symbol, weighted by their traction. "
        "A contested ticker fragments discovery and diverts attention to impostors."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("pumpfun", "dexscreener", "geckoterminal")
    earliest_seconds = 0.0
    min_evidence = 1
    default_midpoint = 2.0
    default_steepness = 0.7
    gameability = (
        "Nothing a team can do here helps them, which is unusual and makes this metric "
        "unusually reliable. The one distortion is a team launching decoys under its "
        "own ticker, which harms only itself."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        symbol = (ctx.token.symbol or "").upper().strip()
        if not symbol:
            return None, 0, "no symbol"
        collisions: list[dict] = ctx.extra.get("ticker_collisions", [])
        if not collisions:
            # Fall back to counting exact symbol matches in the recent corpus.
            corpus_hits = sum(
                1 for c in ctx.recent_narratives if symbol and symbol in c.upper().split()
            )
            if not ctx.recent_narratives:
                return None, 0, "no collision data available"
            return float(corpus_hits), max(1, len(ctx.recent_narratives) // 20), (
                f"{corpus_hits} symbol matches in recent corpus"
            )

        weighted = 0.0
        for other in collisions:
            liquidity = float(other.get("liquidity_usd") or 0.0)
            # A dead namesake costs nothing; a live one with real liquidity
            # is actively taking the attention.
            weighted += 0.2 + saturating(liquidity, scale=20_000.0)
        return weighted, len(collisions), f"{len(collisions)} live tokens share ${symbol}"


class LaunchTimingQuality(Metric):
    """Whether the token launched into a market that was paying attention.

    Identical tokens launched at 14:00 UTC on a Wednesday and at 04:00 UTC on a
    Sunday have very different outcomes, because the audience is not there in the
    second case. This is measurable in real time from the launch feed itself:
    concurrent launch volume, aggregate liquidity flowing into new tokens, and the
    recent graduation rate together describe whether there is demand to compete
    for. Nothing about the token itself changes it, which is exactly why it is
    ungameable and why it belongs in the composite as a conditioner.
    """

    id = "launch_timing_quality"
    name = "Launch timing quality"
    family = "narrative"
    thesis = (
        "Market-wide receptiveness at the moment of launch: recent graduation rate and "
        "capital flowing into new tokens, against launch congestion. The same token "
        "succeeds or fails on this alone."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("pumpfun", "dexscreener")
    earliest_seconds = 0.0
    min_evidence = 5
    default_midpoint = 0.35
    default_steepness = 7.0
    gameability = (
        "A team can choose when to launch, which is not gaming the metric but "
        "responding to it correctly. No adversarial manipulation exists: a team cannot "
        "make the market receptive, and if it could, the metric would be reporting "
        "something true."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        regime: dict = ctx.extra.get("market_regime", {})
        if not regime:
            return None, 0, "no market regime data"

        grad_rate = regime.get("graduation_rate_24h")
        launches_per_hour = regime.get("launches_per_hour")
        new_token_inflow = regime.get("new_token_inflow_usd_1h")
        sample = int(regime.get("sample_size", 0))
        if grad_rate is None or launches_per_hour is None:
            return None, sample, "regime data incomplete"

        # Graduation rate is the cleanest measure of whether launches are working.
        demand = clamp(float(grad_rate) / 0.02)
        # Congestion: more concurrent launches means attention is divided.
        congestion = saturating(float(launches_per_hour), scale=400.0)
        inflow = saturating(float(new_token_inflow or 0.0), scale=750_000.0)

        score = clamp(0.5 * demand + 0.3 * inflow + 0.2 * (1.0 - congestion))
        return score, max(sample, 5), (
            f"grad rate {grad_rate:.3%}, {launches_per_hour:.0f} launches/hr"
        )


class DescriptionSubstance(Metric):
    """Whether anyone put thought into the launch itself.

    Spam launches are produced by scripts a thousand at a time: empty
    descriptions, no socials, a stock image, a name pulled from a word list. The
    tokens that go anywhere almost always show a few minutes of human effort at
    the moment of creation. This is the cheapest possible filter and it removes an
    enormous share of the launch feed before any expensive analysis runs, which
    matters when the feed is thousands of tokens an hour.
    """

    id = "description_substance"
    name = "Launch description substance"
    family = "narrative"
    thesis = (
        "Composite of description length and specificity, presence of working socials, "
        "and non-generic naming. Script-generated spam launches fail all of these and "
        "make up most of the feed."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("pumpfun", "moonshot", "apestore", "believe")
    earliest_seconds = 0.0
    min_evidence = 1
    default_midpoint = 0.4
    default_steepness = 8.0
    gameability = (
        "Trivially gameable in isolation — anyone can write a paragraph and add a "
        "Telegram link. Counter-measure: it is used as a cheap pre-filter and given "
        "low weight in the composite, never as a positive thesis on its own. Its value "
        "is in what it excludes, not what it endorses."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.launch is None:
            return None, 0, "no launch record"
        launch = ctx.launch

        description = (launch.description or "").strip()
        desc_tokens = tokens(description)
        length_component = clamp(len(desc_tokens) / 25.0)
        unique_component = (
            len(set(desc_tokens)) / len(desc_tokens) if desc_tokens else 0.0
        )

        socials = sum(1 for s in (launch.twitter, launch.telegram, launch.website) if s)
        social_component = clamp(socials / 2.0)

        name = (launch.token.name or "").strip()
        symbol = (launch.token.symbol or "").strip()
        naming_component = 0.0
        if name and symbol:
            naming_component += 0.5
            # A name that is not merely the symbol repeated shows some thought.
            if name.upper() != symbol.upper() and len(name) > len(symbol):
                naming_component += 0.5

        has_image = 1.0 if launch.image_uri else 0.0

        score = clamp(
            0.30 * length_component
            + 0.15 * unique_component
            + 0.30 * social_component
            + 0.15 * naming_component
            + 0.10 * has_image
        )
        return score, 1, (
            f"{len(desc_tokens)} description words, {socials} socials, image={bool(launch.image_uri)}"
        )


__all__ = [
    "DescriptionSubstance",
    "LaunchTimingQuality",
    "MetaAlignment",
    "NarrativeNovelty",
    "TickerContention",
]
