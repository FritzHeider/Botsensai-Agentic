"""On-chain social topology metrics.

Holder count is reported by every API and is nearly worthless, because one person
with a script can be two hundred holders before the chart has a second candle.
What no API reports is the *shape* of the holder set: who funded whom, who was
already here before this token existed, and whether the people at the top are
one person wearing hats.

These metrics reconstruct that shape from raw trade and balance data, which means
they do not depend on any vendor's proprietary wallet labels and cannot be
switched off by a vendor changing its pricing.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import timedelta

import networkx as nx

from botsensai.metrics.base import Metric, MetricContext
from botsensai.models import Direction, HolderRecord, Side
from botsensai.util.stats import (
    clamp,
    effective_number,
    gini,
    round_number_share,
    saturating,
    shannon_entropy,
    wilson_lower_bound,
)

#: Solana's canonical burn address. Supply sent here is gone, not held.
BURN_ADDRESS = "1nc1nerator11111111111111111111111111111111"

#: Labels a collector applies to holders that are programs rather than people:
#: the bonding-curve associated token account, AMM pool vaults, and burn sinks.
PROGRAM_RESERVE_LABELS = frozenset(
    {"pool", "vault", "curve", "bonding_curve", "burn", "program", "reserve", "amm"}
)


def tradeable_holders(ctx: MetricContext) -> list[HolderRecord]:
    """Holders that are actual counterparties, with reserves removed.

    Concentration figures computed without this are garbage, and predictably so:
    pre-graduation the bonding-curve account itself holds most of the supply, so
    a naive top-10 share reads near 100% for every healthy token on the platform.
    Pool vaults and the burn address cause the same distortion post-graduation.

    Shares are renormalized over what is left, so a "40% of supply" figure means
    40% of the supply that can actually be sold into the market.
    """
    keep: list[HolderRecord] = []
    for holder in ctx.holders:
        if holder.share_of_supply <= 0:
            continue
        if holder.wallet == BURN_ADDRESS:
            continue
        if PROGRAM_RESERVE_LABELS & {label.lower() for label in holder.labels}:
            continue
        keep.append(holder)

    total = sum(h.share_of_supply for h in keep)
    if total <= 0:
        return []
    if abs(total - 1.0) < 1e-6:
        return keep
    return [
        h.model_copy(update={"share_of_supply": min(1.0, h.share_of_supply / total)})
        for h in keep
    ]


class FunderGraphDispersion(Metric):
    """How many genuinely independent wallets are behind the holder set.

    A cluster of forty wallets all funded from the same source address is one
    buyer, not forty, and it is the standard way an insider allocation is made to
    look like organic distribution. Building the funding graph and counting
    *connected components* rather than addresses collapses each cluster back to
    the single actor it represents. This is the metric that most directly answers
    "is this distribution real", and no public API exposes it.
    """

    id = "funder_graph_dispersion"
    name = "Funder graph dispersion"
    family = "onchain_topology"
    thesis = (
        "Effective number of independent funding clusters behind the top holders, "
        "computed from the funding graph. Sybil clusters collapse to one node, so this "
        "measures actual buyer count rather than address count."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 180.0
    min_evidence = 8
    default_midpoint = 14.0
    default_steepness = 0.14
    gameability = (
        "An operator can launder funding through a CEX so wallets share no on-chain "
        "funder. Counter-measure: CEX-withdrawal wallets are detected by their funder "
        "being a known high-degree hot wallet, and those are excluded from clustering "
        "rather than treated as one actor; the residual signal is timing co-movement, "
        "which `bundle_supply_share` captures independently."
    )

    @staticmethod
    def _funder_degree(holders: Sequence[HolderRecord]) -> Counter[str]:
        """How many of this token's holders each funder is behind.

        Degree is what separates a shared exchange hot wallet (funds thousands
        of unrelated users, so a small share of any one token's holders) from a
        private distributor (funds a large share of exactly this token).
        """
        degree: Counter[str] = Counter()
        for h in holders:
            if h.funded_by:
                degree[h.funded_by] += 1
        return degree

    @classmethod
    def _funding_graph(cls, holders: Sequence[HolderRecord]) -> nx.Graph:
        funder_degree = cls._funder_degree(holders)
        total = len(holders)
        graph = nx.Graph()
        for h in holders:
            graph.add_node(h.wallet, weight=h.share_of_supply)

        for h in holders:
            if not h.funded_by:
                continue
            degree = funder_degree[h.funded_by]
            # Only link the distributors; linking exchanges would collapse every
            # unrelated user of that exchange into one apparent actor.
            if degree >= 2 and (degree / total) >= 0.08:
                graph.add_node(f"funder:{h.funded_by}", weight=0.0)
                graph.add_edge(h.wallet, f"funder:{h.funded_by}")
        return graph

    @staticmethod
    def _cluster_shares(graph: nx.Graph) -> list[float]:
        """Supply share held by each connected component, funder nodes excluded."""
        shares: list[float] = []
        for comp in nx.connected_components(graph):
            share = sum(
                graph.nodes[n].get("weight", 0.0) for n in comp if not n.startswith("funder:")
            )
            if share > 0:
                shares.append(share)
        return shares

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        holders = tradeable_holders(ctx)
        if len(holders) < 6:
            return None, len(holders), "fewer than 6 holders"

        cluster_shares = self._cluster_shares(self._funding_graph(holders))
        if not cluster_shares:
            return None, len(holders), "no positive-share clusters"

        effective = effective_number(cluster_shares)
        return effective, len(holders), (
            f"{len(holders)} holders collapse to {len(cluster_shares)} clusters, "
            f"{effective:.1f} effective"
        )


class FreshWalletRatio(Metric):
    """Share of buyers that had never traded anything before this token.

    Some fresh wallets are genuinely new users, which is healthy. A *majority* of
    fresh wallets means the buy side is manufactured, because real discovery
    brings in wallets with history — people who have traded before and are now
    trading this. The ratio is computed against each wallet's own prior activity
    strictly before this token existed, which is both the honest construction and
    the one that survives a backtest.
    """

    id = "fresh_wallet_ratio"
    name = "Fresh wallet ratio"
    family = "onchain_topology"
    thesis = (
        "Fraction of buyers with no prior on-chain trading history. A buy side made "
        "mostly of wallets born minutes ago is manufactured demand, not discovery."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 120.0
    min_evidence = 10
    default_midpoint = 0.45
    default_steepness = 9.0
    gameability = (
        "An operator can pre-age wallets by having them make trivial trades weeks in "
        "advance. Counter-measure: prior-activity depth is weighted, so a wallet with "
        "three lifetime dust trades counts as barely-aged rather than established, and "
        "pre-aging a fleet has a real carrying cost that most launches will not pay."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        buyers = {t.wallet for t in ctx.trades if t.side is Side.BUY}
        if len(buyers) < 8:
            return None, len(buyers), "fewer than 8 distinct buyers"

        weighted_fresh = 0.0
        for wallet in buyers:
            prior = ctx.wallet_priors.get(wallet)
            if prior is None:
                # Unknown history is not the same as no history; fall back to the
                # holder record's wallet age when we have it.
                age = next(
                    (h.wallet_age_seconds for h in ctx.holders if h.wallet == wallet),
                    None,
                )
                if age is None:
                    continue
                weighted_fresh += 1.0 if age < 86400.0 else 0.0
                continue
            # 0 prior trades = fully fresh; 20+ = fully established.
            weighted_fresh += clamp(1.0 - math.log1p(prior) / math.log(21.0))

        ratio = weighted_fresh / len(buyers)
        return ratio, len(buyers), f"{len(buyers)} buyers, weighted fresh {weighted_fresh:.1f}"


class SniperSupplyShare(Metric):
    """Share of supply captured in the opening moments by wallets that always do this.

    Snipers are not a moral problem, they are a mechanical one: supply held by
    bots that entered in the first slots is supply that will be sold into the
    first sign of retail interest, which caps the move before it starts. The
    distinction that matters and that no API draws is between *snipers* and
    *early buyers* — a wallet that bought in slot 3 of this token and slot 3 of
    two hundred others is a bot; a wallet that bought early because it was paying
    attention is a customer.
    """

    id = "sniper_supply_share"
    name = "Sniper supply share"
    family = "onchain_topology"
    thesis = (
        "Supply share held by wallets that bought within the first slots and have a "
        "history of doing exactly that. This is the overhang that caps the first move."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 60.0
    min_evidence = 5
    default_midpoint = 0.18
    default_steepness = 14.0
    gameability = (
        "Snipers can spread entries across more slots to avoid the window. "
        "Counter-measure: widening their window costs them their edge against each "
        "other, so the behaviour is self-limiting; residual late-sniping shows up in "
        "`bundle_supply_share` and in the fee-percentile metric."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.launch is None:
            return None, 0, "no launch record"
        trades = sorted(
            [t for t in ctx.trades if t.side is Side.BUY],
            key=lambda t: (t.slot if t.slot is not None else 0, t.as_of),
        )
        if len(trades) < 5:
            return None, len(trades), "fewer than 5 buys"

        cutoff = ctx.launch.created_at + timedelta(seconds=20)
        first_slot = next((t.slot for t in trades if t.slot is not None), None)

        snipers: set[str] = set()
        for t in trades:
            in_time_window = t.as_of <= cutoff
            in_slot_window = (
                first_slot is not None and t.slot is not None and (t.slot - first_slot) <= 12
            )
            if in_time_window or in_slot_window:
                snipers.add(t.wallet)

        if not snipers:
            return 0.0, len(trades), "no wallets in the opening window"

        # Weight each sniper by how much this looks like their profession.
        holder_share = {h.wallet: h.share_of_supply for h in tradeable_holders(ctx)}
        weighted = 0.0
        for wallet in snipers:
            share = holder_share.get(wallet, 0.0)
            prior = ctx.wallet_priors.get(wallet, 0)
            # A wallet with hundreds of prior trades that arrived in slot 2 is a
            # bot; a first-timer that arrived in slot 2 is probably the deployer's
            # friend, which is a different (and separately measured) problem.
            professional = clamp(math.log1p(prior) / math.log(201.0))
            weighted += share * (0.4 + 0.6 * professional)

        return weighted, len(trades), f"{len(snipers)} opening-window wallets"


class BundleSupplyShare(Metric):
    """Supply acquired inside atomic same-slot bundles.

    Buying a large share of a token's supply in a single Jito bundle is the
    dominant way an insider allocation gets established while looking like
    twenty separate purchases in the trade feed. Detecting it requires slot-level
    co-occurrence analysis that the aggregator APIs do not perform: the tell is
    many distinct wallets, identical slot, and suspiciously round or identical
    sizes.
    """

    id = "bundle_supply_share"
    name = "Bundle supply share"
    family = "onchain_topology"
    thesis = (
        "Share of supply acquired by wallets transacting atomically in the same slot. "
        "This is a single actor's allocation disguised as broad distribution, and it "
        "is the supply most likely to be dumped."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("solana_rpc", "helius", "jito")
    earliest_seconds = 60.0
    min_evidence = 6
    default_midpoint = 0.15
    default_steepness = 16.0
    gameability = (
        "Bundles can be split across consecutive slots to break exact co-occurrence. "
        "Counter-measure: the size-similarity and round-number terms fire regardless of "
        "slot alignment, and splitting across slots surrenders the atomicity that made "
        "bundling worth doing, exposing the operator to being front-run."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        buys = [t for t in ctx.trades if t.side is Side.BUY]
        if len(buys) < 6:
            return None, len(buys), "fewer than 6 buys"

        explicit = {t.wallet for t in buys if t.is_bundled}
        by_slot: dict[int, list] = defaultdict(list)
        for t in buys:
            if t.slot is not None:
                by_slot[t.slot].append(t)

        suspicious_wallets: set[str] = set(explicit)
        for group in by_slot.values():
            wallets = {t.wallet for t in group}
            if len(wallets) < 3:
                continue
            sizes = [t.amount_native for t in group if t.amount_native > 0]
            if not sizes:
                continue
            # Identical or round sizes across distinct wallets in one slot is the
            # fingerprint; organic co-arrival in a slot has varied sizes.
            size_spread = (max(sizes) - min(sizes)) / max(1e-9, max(sizes))
            roundness = round_number_share(sizes)
            if size_spread < 0.15 or roundness > 0.6:
                suspicious_wallets |= wallets

        if not suspicious_wallets:
            return 0.0, len(buys), "no bundle signature detected"

        holder_share = {h.wallet: h.share_of_supply for h in tradeable_holders(ctx)}
        share = sum(holder_share.get(w, 0.0) for w in suspicious_wallets)
        if share == 0.0 and ctx.holders:
            # Holders not yet loaded for these wallets; fall back to notional share.
            total_native = sum(t.amount_native for t in buys) or 1.0
            share = sum(t.amount_native for t in buys if t.wallet in suspicious_wallets) / total_native

        return share, len(buys), f"{len(suspicious_wallets)} wallets show bundle signature"


class SmartWalletParticipation(Metric):
    """Presence of wallets with a demonstrated history of being early and right.

    This reconstructs from raw data what GMGN and Nansen sell as a labelled feed.
    A wallet earns standing by having previously bought tokens that subsequently
    ran, before they ran — measured strictly on trades that closed before the
    current token existed, which is what keeps it out of look-ahead territory.
    Building it in-house means the signal cannot be revoked by a vendor and is
    not shared with everyone else paying the same subscription.
    """

    id = "smart_wallet_participation"
    name = "Smart wallet participation"
    family = "onchain_topology"
    thesis = (
        "Weighted presence of wallets whose prior trades, closed before this token "
        "launched, show consistent early entries into tokens that subsequently ran."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 120.0
    min_evidence = 6
    default_midpoint = 0.06
    default_steepness = 24.0
    gameability = (
        "A known-good wallet can be paid to buy a small amount as an endorsement. "
        "Counter-measure: participation is weighted by position size relative to that "
        "wallet's own typical size, so a token dust-bought by a smart wallet scores "
        "near zero while a genuine conviction position scores."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        buys = [t for t in ctx.trades if t.side is Side.BUY]
        if len(buys) < 5:
            return None, len(buys), "fewer than 5 buys"

        scores: dict[str, float] = ctx.extra.get("wallet_skill", {})
        typical: dict[str, float] = ctx.extra.get("wallet_typical_size", {})
        if not scores:
            return None, 0, "no wallet skill profiles available"

        by_wallet: dict[str, float] = defaultdict(float)
        for t in buys:
            by_wallet[t.wallet] += t.amount_native
        total_native = sum(by_wallet.values()) or 1.0

        weighted = 0.0
        smart_count = 0
        for wallet, native in by_wallet.items():
            skill = scores.get(wallet)
            if skill is None or skill <= 0.5:
                continue
            smart_count += 1
            baseline = typical.get(wallet, 0.0)
            # Conviction: is this a normal-sized position for them, or a token dust buy?
            conviction = 1.0 if baseline <= 0 else clamp(native / max(1e-9, baseline), 0.0, 2.0) / 2.0
            weighted += (native / total_native) * (skill - 0.5) * 2.0 * (0.3 + 0.7 * conviction)

        if smart_count == 0:
            return 0.0, len(by_wallet), "no profiled skilled wallets present"
        return weighted, len(by_wallet), f"{smart_count} skilled wallets participating"


class EarlyHolderRetention(Metric):
    """Whether the people who bought first are still holding.

    The cleanest possible statement of confidence: the cohort with the lowest
    cost basis, the ones who could take profit at any moment, choosing not to.
    Every rug and every failed launch shows the same shape — the early cohort
    leaves within minutes. Cohort retention is standard practice in consumer
    analytics and almost absent from crypto tooling, which reports holder count
    (which rises as the early cohort sells to bagholders) instead.
    """

    id = "early_holder_retention"
    name = "Early holder retention"
    family = "onchain_topology"
    thesis = (
        "Fraction of the first-five-minute buyer cohort still holding a meaningful "
        "position. The lowest-cost-basis cohort declining to sell is the strongest "
        "available statement of insider confidence."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 420.0
    min_evidence = 6
    default_midpoint = 0.55
    default_steepness = 8.0
    gameability = (
        "The team can hold its own allocation to inflate retention. Counter-measure: "
        "wallets in the deployer's funding cluster are excluded via the same graph "
        "`funder_graph_dispersion` builds, so team holdings do not count as retention."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.launch is None:
            return None, 0, "no launch record"
        if ctx.age_seconds < 360.0:
            return None, 0, "cohort window not yet closed"

        cohort_cutoff = ctx.launch.created_at + timedelta(seconds=300)
        cohort = {
            t.wallet
            for t in ctx.trades
            if t.side is Side.BUY and t.as_of <= cohort_cutoff
        }
        if len(cohort) < 5:
            return None, len(cohort), "fewer than 5 wallets in the early cohort"

        # Exclude anything in the deployer's cluster: their holding is not a vote.
        deployer = ctx.launch.deployer
        excluded: set[str] = {deployer} if deployer else set()
        deployer_funder = next(
            (h.funded_by for h in tradeable_holders(ctx) if deployer and h.wallet == deployer),
            None,
        )
        if deployer_funder:
            excluded |= {
                h.wallet for h in tradeable_holders(ctx) if h.funded_by == deployer_funder
            }

        cohort -= excluded
        if len(cohort) < 4:
            return None, len(cohort), "early cohort is entirely team-affiliated"

        holder_share = {h.wallet: h.share_of_supply for h in tradeable_holders(ctx)}
        still_holding = sum(1 for w in cohort if holder_share.get(w, 0.0) > 1e-6)

        # Wilson bound: 4-of-5 should not read as 80% confidence.
        retention = wilson_lower_bound(still_holding, len(cohort))
        return retention, len(cohort), f"{still_holding}/{len(cohort)} early buyers still hold"


class HolderDistributionHealth(Metric):
    """Supply concentration measured on independent actors rather than addresses.

    Every explorer shows a top-10 holder percentage. It is close to meaningless
    on a token where one actor controls forty addresses. Recomputing Gini over
    *funding clusters* rather than raw addresses gives the number the top-10
    percentage was supposed to give, and the gap between the two is itself
    informative: a token whose address-level distribution looks fine but whose
    cluster-level distribution does not has been deliberately dressed up.
    """

    id = "holder_distribution_health"
    name = "Holder distribution health"
    family = "onchain_topology"
    thesis = (
        "Gini of supply across funding clusters rather than addresses. The divergence "
        "between address-level and cluster-level concentration reveals deliberately "
        "disguised insider allocations."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 180.0
    min_evidence = 8
    default_midpoint = 0.5
    default_steepness = 6.0
    gameability = (
        "Splitting a position across many funded-from-CEX wallets breaks clustering. "
        "Counter-measure: the disguise term explicitly rewards the case where address "
        "and cluster concentration agree, so a launch that goes to unusual lengths to "
        "look evenly distributed is scored more sceptically, not less."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        holders = tradeable_holders(ctx)
        if len(holders) < 8:
            return None, len(holders), "fewer than 8 holders"

        address_gini = gini([h.share_of_supply for h in holders])

        clusters: dict[str, float] = defaultdict(float)
        for h in holders:
            key = h.funded_by or f"solo:{h.wallet}"
            clusters[key] += h.share_of_supply
        cluster_gini = gini(list(clusters.values()))

        # Both low is healthy. Cluster >> address means disguised concentration.
        disguise_gap = max(0.0, cluster_gini - address_gini)
        health = clamp(1.0 - cluster_gini) * clamp(1.0 - 2.0 * disguise_gap)

        breadth = saturating(float(len(clusters)), scale=25.0)
        evenness = shannon_entropy(list(clusters.values()), normalize=True)
        score = 0.5 * health + 0.3 * evenness + 0.2 * breadth
        return score, len(holders), (
            f"address gini {address_gini:.2f}, cluster gini {cluster_gini:.2f}, "
            f"{len(clusters)} clusters"
        )


__all__ = [
    "BundleSupplyShare",
    "EarlyHolderRetention",
    "FreshWalletRatio",
    "FunderGraphDispersion",
    "HolderDistributionHealth",
    "SmartWalletParticipation",
    "SniperSupplyShare",
]
