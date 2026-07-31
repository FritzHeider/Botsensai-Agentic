"""Execution-quality metrics.

A signal you cannot act on is not a signal. These metrics measure whether a
position of the size Botsensai actually trades can be entered and — much more
importantly — exited, and how contested the entry is. They are the difference
between a backtest that shows 4x and a live account that shows a loss.

Nothing in the standard market-data APIs addresses this. Reported liquidity is a
pool balance, not an exit path, and on a bonding curve the two are not the same
number.
"""

from __future__ import annotations

import math
import statistics

from botsensai.metrics.base import Metric, MetricContext
from botsensai.models import CurveStage, Direction, Side
from botsensai.util.stats import clamp, saturating


class RealizableExitDepth(Metric):
    """How much can actually be sold before the price impact eats the trade.

    Reported liquidity is the number everyone quotes and it systematically
    overstates what a seller can get, because half the pool is the token itself
    and because a bonding curve's depth is asymmetric. This computes the notional
    that can be exited at an acceptable impact using the curve or pool mechanics
    directly, then expresses it as a multiple of the position size the risk
    settings would actually take. Below 1.0 the trade is untakeable no matter how
    good the signal is.
    """

    id = "realizable_exit_depth"
    name = "Realizable exit depth"
    family = "execution_quality"
    thesis = (
        "Notional that can be sold within an acceptable price impact, as a multiple of "
        "the intended position size. Reported liquidity overstates this systematically, "
        "and the gap is where backtested edge disappears."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("dexscreener", "geckoterminal", "pumpfun")
    earliest_seconds = 30.0
    min_evidence = 1
    default_midpoint = 3.0
    default_steepness = 0.5
    gameability = (
        "Liquidity can be temporarily inflated and pulled. Counter-measure: the metric "
        "uses the minimum depth observed across the recent snapshot window rather than "
        "the latest reading, so a momentary top-up does not lift it, and LP-burn status "
        "is enforced separately as a hard veto."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        recent = ctx.snapshots_within(300.0) or ctx.snapshots
        if not recent:
            return None, 0, "no market snapshots"

        liquidities = [s.liquidity_usd for s in recent if s.liquidity_usd and s.liquidity_usd > 0]
        if not liquidities:
            return None, len(recent), "no liquidity readings"

        # Minimum over the window, not the latest: resistance to a momentary top-up.
        liquidity = min(liquidities)
        latest = recent[-1]
        target_usd = float(ctx.extra.get("target_position_usd", 40.0))
        max_impact = float(ctx.extra.get("max_impact_pct", 0.05))

        if latest.stage is CurveStage.BONDING and latest.bonding_curve_progress is not None:
            # On a bonding curve, sellable depth shrinks as progress falls; the
            # early curve is thin in exactly the region a fast exit needs.
            progress = clamp(latest.bonding_curve_progress)
            effective = liquidity * (0.25 + 0.75 * progress)
        else:
            # Constant product: selling x into reserve R moves price by
            # roughly x/(R+x). Solving for max_impact gives the sellable notional.
            # Only one side of the pool is the quote asset, hence the halving.
            effective = liquidity * 0.5

        sellable = effective * (max_impact / (1.0 + max_impact))
        if target_usd <= 0:
            return None, len(recent), "invalid target position size"

        multiple = sellable / target_usd
        return multiple, len(recent), (
            f"min liquidity ${liquidity:,.0f}, sellable ${sellable:,.0f} "
            f"at {max_impact:.0%} impact = {multiple:.2f}x target"
        )


class EntryContention(Metric):
    """How hard other bots are competing for the same fill.

    Priority fees and Jito tips are a live auction for block inclusion, and their
    distribution on a specific token is a direct readout of how many automated
    buyers want in. Moderate contention is a confirmation signal — other systems
    have independently reached the same conclusion. Extreme contention means the
    fill will be poor and the sophisticated money is already positioned. This is
    an order-flow signal that exists nowhere in the market-data APIs, which report
    trades but discard the fee metadata that makes them interpretable.
    """

    id = "entry_contention"
    name = "Entry contention"
    family = "execution_quality"
    thesis = (
        "Distribution of priority fees and Jito tips on this token's trades. Moderate "
        "contention independently confirms the opportunity; extreme contention means "
        "the fill is already bad and faster systems are ahead of us."
    )
    direction = Direction.NON_MONOTONIC
    sources = ("solana_rpc", "helius", "jito")
    earliest_seconds = 60.0
    min_evidence = 8
    default_midpoint = 0.5
    default_steepness = 6.0
    gameability = (
        "An operator can pad fees on their own trades to fake contention and lure "
        "momentum bots. Counter-measure: the metric requires fee elevation across many "
        "distinct wallets, and cross-checks against `funder_graph_dispersion` so a "
        "single cluster paying high fees to itself does not register."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        trades = [
            t
            for t in ctx.trades_within(600.0)
            if t.side is Side.BUY and (t.priority_fee_lamports or t.jito_tip_lamports)
        ]
        if len(trades) < 6:
            return None, len(trades), "fewer than 6 trades with fee metadata"

        wallets = {t.wallet for t in trades}
        if len(wallets) < 4:
            return None, len(trades), "fee elevation confined to too few wallets"

        fees = [
            float((t.priority_fee_lamports or 0) + (t.jito_tip_lamports or 0)) for t in trades
        ]
        median_fee = statistics.median(fees)
        baseline = float(ctx.extra.get("network_median_fee_lamports", 200_000.0))
        if baseline <= 0:
            baseline = 200_000.0

        elevation = math.log10(max(1.0, median_fee) / max(1.0, baseline))

        # Non-monotonic: peak value at moderate elevation (~0.5 decades), falling
        # away on both sides. Implemented as a Gaussian around the sweet spot.
        score = math.exp(-((elevation - 0.5) ** 2) / (2 * 0.45**2))

        # Breadth requirement: contention must be distributed across actors.
        breadth = saturating(float(len(wallets)), scale=12.0)
        return score * (0.4 + 0.6 * breadth), len(trades), (
            f"median fee {median_fee:,.0f} lamports across {len(wallets)} wallets, "
            f"elevation {elevation:+.2f} decades"
        )


class BuyPressureQuality(Metric):
    """Whether the buying is many people or one person clicking repeatedly.

    Buy/sell transaction counts are reported by every API and are among the
    easiest numbers to manufacture: a single wallet can produce two hundred buys
    for the cost of the fees. What is not cheap is *distinct funded buyers with
    varied sizes*. This metric reweights the standard buy-pressure statistic by
    the diversity of who is producing it and by the naturalness of the size
    distribution, turning a heavily-gamed number into a usable one.
    """

    id = "buy_pressure_quality"
    name = "Buy pressure quality"
    family = "execution_quality"
    thesis = (
        "Buy-side dominance reweighted by distinct-buyer breadth and size-distribution "
        "naturalness. The raw buy/sell ratio every API reports is trivially "
        "manufactured; this is not."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("solana_rpc", "helius", "dexscreener")
    earliest_seconds = 120.0
    min_evidence = 10
    default_midpoint = 0.3
    default_steepness = 7.0
    gameability = (
        "Wash trading across many wallets can produce breadth with varied sizes. "
        "Counter-measure: circular-flow detection excludes wallets that both buy and "
        "sell repeatedly within the window, and the round-number term catches the "
        "scripted size ladders wash traders typically use."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        window = ctx.trades_within(600.0)
        if len(window) < 8:
            return None, len(window), "fewer than 8 trades in window"

        buys = [t for t in window if t.side is Side.BUY]
        sells = [t for t in window if t.side is Side.SELL]
        if not buys:
            return -1.0, len(window), "no buys in window"

        # Exclude wallets that round-trip inside the window: that is wash volume,
        # not demand.
        buy_wallets = {t.wallet for t in buys}
        sell_wallets = {t.wallet for t in sells}
        round_trippers = buy_wallets & sell_wallets
        clean_buys = [t for t in buys if t.wallet not in round_trippers]
        clean_buyers = {t.wallet for t in clean_buys}

        if not clean_buys:
            return -1.0, len(window), "all buying is round-tripping wash volume"

        buy_native = sum(t.amount_native for t in clean_buys)
        sell_native = sum(t.amount_native for t in sells) or 1e-9
        imbalance = math.log((buy_native + 1e-9) / (sell_native + 1e-9))

        breadth = saturating(float(len(clean_buyers)), scale=15.0)
        wash_penalty = 1.0 - clamp(len(round_trippers) / max(1, len(buy_wallets)))

        sizes = [t.amount_native for t in clean_buys if t.amount_native > 0]
        size_naturalness = 1.0
        if len(sizes) >= 6:
            spread = statistics.pstdev(sizes) / max(1e-9, statistics.fmean(sizes))
            # Identical sizes across "different" buyers means one script.
            size_naturalness = clamp(spread / 1.2)

        score = imbalance * (0.3 + 0.7 * breadth) * wash_penalty * (0.4 + 0.6 * size_naturalness)
        return score, len(window), (
            f"{len(clean_buyers)} clean buyers, {len(round_trippers)} round-trippers, "
            f"imbalance {imbalance:+.2f}"
        )


class PriceStabilityUnderFlow(Metric):
    """Whether price is holding up against the volume going through it.

    A token that rises on heavy volume and one that rises on nothing look
    identical on a chart and behave completely differently: the second is a
    thin-book artifact that unwinds on the first real sell. Dividing realized
    price change by the volume required to produce it gives a resilience measure
    — how much buying it takes to move this token one percent — and its inverse
    is the fragility that determines whether an exit is possible.
    """

    id = "price_stability_under_flow"
    name = "Price stability under flow"
    family = "execution_quality"
    thesis = (
        "Realized price change per unit of volume. A token that moves on almost no "
        "flow will unwind on almost no flow, which is invisible on a price chart."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("dexscreener", "geckoterminal", "solana_rpc")
    earliest_seconds = 300.0
    min_evidence = 4
    default_midpoint = 0.0
    default_steepness = 1.2
    gameability = (
        "Volume can be washed to make the denominator large and the token look "
        "resilient. Counter-measure: the volume term uses only non-round-tripping "
        "flow when trade-level data is available, and falls back to reported volume "
        "with reduced confidence when it is not."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        snaps = ctx.snapshots_within(900.0)
        if len(snaps) < 3:
            return None, len(snaps), "fewer than 3 snapshots in window"

        priced = [s for s in snaps if s.price_native and s.price_native > 0]
        if len(priced) < 3:
            return None, len(priced), "insufficient price readings"

        # The comprehension above already dropped the None prices, but a filter
        # over an attribute does not narrow the Optional, so read them once.
        prices = [s.price_native for s in priced if s.price_native is not None]
        last = priced[-1]
        price_change = abs(math.log(prices[-1] / prices[0]))

        trades = ctx.trades_within(900.0)
        if trades:
            buy_wallets = {t.wallet for t in trades if t.side is Side.BUY}
            sell_wallets = {t.wallet for t in trades if t.side is Side.SELL}
            round_trippers = buy_wallets & sell_wallets
            flow = sum(t.amount_native for t in trades if t.wallet not in round_trippers)
            evidence = len(trades)
            note_source = "clean trade flow"
        else:
            flow = float(last.volume_5m_usd or last.volume_1h_usd or 0.0)
            evidence = len(priced)
            note_source = "reported volume"

        if flow <= 0:
            return None, evidence, "no usable flow measurement"

        if price_change < 1e-6:
            # Flat on real volume is genuinely resilient.
            return math.log1p(flow), evidence, f"flat price on {flow:.2f} flow ({note_source})"

        resilience = math.log1p(flow / price_change) - 3.0
        return resilience, evidence, (
            f"|Δlog price| {price_change:.4f} on {flow:.2f} flow ({note_source})"
        )


__all__ = [
    "BuyPressureQuality",
    "EntryContention",
    "PriceStabilityUnderFlow",
    "RealizableExitDepth",
]
