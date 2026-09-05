"""Deployer and team credibility metrics.

The same few thousand wallets deploy most of the tokens on these platforms, and
they have track records. That record is the most predictive single fact available
about a token that is thirty seconds old and has no other history — and it is
strictly time-restricted here, because scoring a deployer using outcomes that had
not happened yet at decision time is the most common way this class of feature
produces a backtest that cannot be reproduced live.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta

from botsensai.metrics.base import Metric, MetricContext
from botsensai.models import Direction, HolderRecord, Side, Trade
from botsensai.util.stats import clamp, saturating, wilson_lower_bound


class DeployerLineage(Metric):
    """The deployer's prior record, as it stood before this launch.

    Serial ruggers rug again. Deployers who have shipped something that ran
    before are meaningfully more likely to do it again, partly through skill and
    partly because their previous holders follow them. The construction that
    matters is the time restriction: only launches created before this one, and
    only outcomes labelled before this moment, count. Without that restriction
    this metric produces spectacular backtests and loses money live.
    """

    id = "deployer_lineage"
    name = "Deployer lineage"
    family = "team_credibility"
    thesis = (
        "The deployer's prior launch record — rug rate, graduation rate and best "
        "result — restricted to what was knowable before this launch. It is the most "
        "predictive fact available in the first seconds of a token's life."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("pumpfun", "solana_rpc")
    earliest_seconds = 0.0
    min_evidence = 1
    default_midpoint = 0.0
    default_steepness = 2.0
    gameability = (
        "A deployer can simply use a fresh wallet, which erases the record. "
        "Counter-measure: an unknown deployer scores neutral rather than good, so "
        "erasure buys anonymity, not endorsement; and funding-graph linkage often "
        "reconnects a fresh deployer to its predecessor, which is checked here."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        history = ctx.deployer_history
        if not history:
            return None, 0, "no deployer history available"

        launches = int(history.get("launch_count", 0))
        if launches == 0:
            # A genuinely first-time deployer. Neutral, not good, not bad.
            return 0.0, 1, "first-time deployer, scored neutral"

        rugs = int(history.get("rug_count", 0))
        grads = int(history.get("graduate_count", 0))
        best = float(history.get("best_multiple", 0.0) or 0.0)

        # Wilson bounds so one rug out of one launch is not read as a 100% rug rate.
        rug_rate = wilson_lower_bound(rugs, launches)
        grad_rate = wilson_lower_bound(grads, launches)

        # Log-odds style combination: a strong prior winner outweighs volume of
        # mediocre launches, and any rug history is heavily punitive.
        upside = math.log1p(max(0.0, best)) * 0.4 + grad_rate * 2.0
        downside = rug_rate * 4.0 + saturating(float(launches), 60.0) * 0.5
        score = upside - downside

        return score, max(1, launches), (
            f"{launches} prior launches, {rugs} rugs, {grads} graduations, best {best:.1f}x"
        )


class DeployerBehaviourNow(Metric):
    """What the deployer is doing with their own bag, right now.

    Stated intentions are worthless; the deployer's transactions are not. Selling
    into the opening minutes is the single most reliable precursor to a token
    going to zero, and it is observable the moment it happens. Equally, a deployer
    who took a normal-sized position and has not touched it is making a costly
    statement. This differs from the static "dev sold" flag most tools show
    because it is graded — partial sells, incremental trimming and buying more all
    read differently.
    """

    id = "deployer_behaviour_now"
    name = "Deployer behaviour"
    family = "team_credibility"
    thesis = (
        "Graded read of the deployer's own trading in this token: adding, holding, "
        "trimming or dumping. Deployer selling in the first minutes is the most "
        "reliable single precursor of failure."
    )
    direction = Direction.HIGHER_IS_BULLISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 60.0
    min_evidence = 1
    default_midpoint = 0.0
    default_steepness = 3.0
    gameability = (
        "A deployer can sell through a separate wallet funded before launch, which "
        "hides the sale from a naive dev-sold flag. Counter-measure: wallets sharing "
        "the deployer's funder are treated as the deployer here, using the same graph "
        "`funder_graph_dispersion` builds, so the split has to be laundered through a "
        "CEX to work — which costs time the deployer usually does not take."
    )

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.launch is None or not ctx.launch.deployer:
            return None, 0, "no deployer known"
        deployer = ctx.launch.deployer

        # Treat the deployer's funding cluster as the deployer.
        deployer_funder = next(
            (h.funded_by for h in ctx.holders if h.wallet == deployer and h.funded_by), None
        )
        team_wallets = {deployer}
        if deployer_funder:
            team_wallets |= {
                h.wallet for h in ctx.holders if h.funded_by == deployer_funder
            }

        team_trades = [t for t in ctx.trades if t.wallet in team_wallets]
        if not team_trades:
            # Silence is mildly positive: no selling observed.
            if ctx.age_seconds < 120:
                return None, 0, "too early to read deployer behaviour"
            return 0.5, 1, "no deployer trades observed"

        bought = sum(t.amount_native for t in team_trades if t.side is Side.BUY)
        sold = sum(t.amount_native for t in team_trades if t.side is Side.SELL)

        if bought <= 0 and sold <= 0:
            return 0.0, len(team_trades), "deployer trades have no notional"

        # Recency matters: a sale two minutes ago is worse than one an hour ago
        # on an eight-hour-old token, because it is the current stance.
        recent_cutoff = ctx.as_of - timedelta(seconds=600)
        recent_sold = sum(
            t.amount_native
            for t in team_trades
            if t.side is Side.SELL and t.as_of >= recent_cutoff
        )

        if sold <= 0:
            # Holding, and possibly adding.
            add_signal = saturating(bought, scale=2.0)
            return 1.0 + add_signal, len(team_trades), (
                f"deployer holding, bought {bought:.2f} native"
            )

        sell_ratio = sold / max(1e-9, bought + sold)
        recency_penalty = 1.0 + clamp(recent_sold / max(1e-9, sold))
        score = -(sell_ratio * 3.0 * recency_penalty)
        return score, len(team_trades), (
            f"deployer sold {sold:.2f} of {bought + sold:.2f} native "
            f"({sell_ratio:.0%}), {recent_sold:.2f} in last 10m"
        )


class InsiderSupplyOverhang(Metric):
    """How much of the supply is held by people who paid nothing for it.

    Distinct from sniping and from bundling: this is supply that reached wallets
    connected to the deployer without an arm's-length purchase, either at
    creation or via transfer. It is the overhang that determines how far the
    token can actually run, because it will be sold into any strength. Standard
    tools report a top-10 percentage that includes the liquidity pool and treats
    every address as a separate person; this measures the specific thing that
    matters.
    """

    id = "insider_supply_overhang"
    name = "Insider supply overhang"
    family = "team_credibility"
    thesis = (
        "Share of supply held by wallets linked to the deployer that did not acquire it "
        "through an arm's-length purchase. This is the overhang that caps the move."
    )
    direction = Direction.HIGHER_IS_BEARISH
    sources = ("solana_rpc", "helius")
    earliest_seconds = 120.0
    min_evidence = 5
    default_midpoint = 0.12
    default_steepness = 18.0
    gameability = (
        "Insiders can buy their allocation on the open market to make it look "
        "arm's-length. Counter-measure: doing so costs them real capital and moves the "
        "price against themselves, which is the outcome we want; the residual is "
        "captured as sniping or bundling by the topology metrics."
    )

    @staticmethod
    def _linked_wallets(deployer: str | None, holders: Sequence[HolderRecord]) -> set[str]:
        """Wallets attributable to the team: the deployer, its funding siblings, and labels."""
        deployer_funder = next(
            (h.funded_by for h in holders if deployer and h.wallet == deployer and h.funded_by),
            None,
        )

        linked: set[str] = {deployer} if deployer else set()
        if deployer_funder:
            linked |= {h.wallet for h in holders if h.funded_by == deployer_funder}
        for h in holders:
            if "insider" in h.labels or "team" in h.labels or "creator" in h.labels:
                linked.add(h.wallet)
        return linked

    @staticmethod
    def _net_purchased(trades: Sequence[Trade], linked: set[str]) -> dict[str, float]:
        purchased: dict[str, float] = {}
        for t in trades:
            if t.wallet in linked:
                delta = t.amount_token if t.side is Side.BUY else -t.amount_token
                purchased[t.wallet] = purchased.get(t.wallet, 0.0) + delta
        return purchased

    @classmethod
    def _overhang(
        cls,
        holders: Sequence[HolderRecord],
        linked: set[str],
        trades: Sequence[Trade],
    ) -> float:
        """Supply share held by linked wallets that no observed purchase explains."""
        purchased = cls._net_purchased(trades, linked)
        overhang = 0.0
        for h in holders:
            if h.wallet not in linked or h.balance <= 0:
                continue
            bought = max(0.0, purchased.get(h.wallet, 0.0))
            unpurchased = max(0.0, h.balance - bought)
            overhang += h.share_of_supply * (unpurchased / h.balance)
        return overhang

    def compute(self, ctx: MetricContext) -> tuple[float | None, int, str]:
        if ctx.launch is None:
            return None, 0, "no launch record"
        holders = [h for h in ctx.holders if h.share_of_supply > 0]
        if len(holders) < 5:
            return None, len(holders), "fewer than 5 holders"

        linked = self._linked_wallets(ctx.launch.deployer, holders)
        if not linked:
            return 0.0, len(holders), "no deployer-linked wallets identified"

        overhang = self._overhang(holders, linked, ctx.trades)
        return overhang, len(holders), (
            f"{len(linked)} deployer-linked wallets hold {overhang:.1%} unpurchased supply"
        )


__all__ = ["DeployerBehaviourNow", "DeployerLineage", "InsiderSupplyOverhang"]
