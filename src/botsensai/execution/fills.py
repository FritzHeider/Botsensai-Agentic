"""Fill modelling.

This is where honest backtests are won or lost. A naive simulator fills at the
last observed price, instantly, with no fees. Every one of those assumptions is
false on a Solana memecoin, and together they are worth several hundred percent
of apparent annual return.

The model here charges for all of it:

* **Latency.** The price at the moment of decision is not the price at the moment
  of inclusion. Between them sit block time, RPC round-trips and the collector's
  own polling interval, and on a token doubling every ninety seconds that gap is
  the whole trade.
* **Price impact.** On a bonding curve the cost of a buy is an integral over the
  curve, not a spot price. On an AMM it is constant-product. Both are computed
  properly rather than approximated with a flat slippage percentage.
* **Priority fees and tips.** Real, per-transaction, and much larger than the
  trade fees people remember to model.
* **Failure.** Transactions fail. A model where every order fills is a model where
  the strategy never misses the trade it most wanted, which is precisely backwards
  — orders fail most often exactly when everyone wants in.
* **Sandwiching.** A predictable buy into a thin pool gets front-run. Modelled
  probabilistically with an extra impact charge.

The same code path serves the paper broker and the backtester, so a paper result
and a backtest result are directly comparable rather than two different fictions.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import timedelta

from botsensai.config import ExecutionSettings
from botsensai.models import CurveStage, Fill, MarketSnapshot, Order, Side

LAMPORTS_PER_SOL = 1_000_000_000

#: pump.fun-style bonding curve defaults. These are the shape of the curve, not
#: exact protocol constants, and are overridable per-token via `CurveState`.
DEFAULT_VIRTUAL_SOL = 30.0
DEFAULT_VIRTUAL_TOKENS = 1_073_000_000.0


@dataclass
class CurveState:
    """Constant-product state, used for both bonding curves and AMM pools.

    A pump.fun bonding curve is a constant-product market maker with virtual
    reserves, so one implementation covers both venues; only the reserve values
    differ. Modelling it as a curve rather than a fixed slippage percentage
    matters most in exactly the regime we trade — the first minutes, when the
    curve is steepest.
    """

    virtual_sol: float = DEFAULT_VIRTUAL_SOL
    virtual_tokens: float = DEFAULT_VIRTUAL_TOKENS
    real_sol: float = 0.0
    real_tokens: float = 0.0

    @property
    def sol_reserve(self) -> float:
        return self.virtual_sol + self.real_sol

    @property
    def token_reserve(self) -> float:
        return self.virtual_tokens - self.real_tokens

    @property
    def spot_price(self) -> float:
        """SOL per token at the margin."""
        tokens = self.token_reserve
        if tokens <= 0:
            return float("inf")
        return self.sol_reserve / tokens

    def tokens_out_for_sol_in(self, sol_in: float) -> float:
        """Constant product: k = x*y, solve for tokens received."""
        if sol_in <= 0:
            return 0.0
        x, y = self.sol_reserve, self.token_reserve
        if x <= 0 or y <= 0:
            return 0.0
        k = x * y
        new_x = x + sol_in
        new_y = k / new_x
        return max(0.0, y - new_y)

    def sol_out_for_tokens_in(self, tokens_in: float) -> float:
        if tokens_in <= 0:
            return 0.0
        x, y = self.sol_reserve, self.token_reserve
        if x <= 0 or y <= 0:
            return 0.0
        k = x * y
        new_y = y + tokens_in
        new_x = k / new_y
        return max(0.0, x - new_x)

    @classmethod
    def from_snapshot(
        cls, snapshot: MarketSnapshot, native_price_usd: float = 150.0
    ) -> CurveState:
        """Reconstruct plausible reserves from an observed snapshot.

        Exact reserves are rarely in the data we collect, so they are inferred
        from liquidity and price. The inference is conservative — it assumes half
        the reported liquidity is the quote asset, which is the standard
        constant-product split and understates depth if anything.
        """
        price_native = snapshot.price_native
        if not price_native or price_native <= 0:
            if snapshot.price_usd and snapshot.price_usd > 0 and native_price_usd > 0:
                price_native = snapshot.price_usd / native_price_usd
            else:
                price_native = 1e-8

        liquidity_usd = snapshot.liquidity_usd or 0.0
        sol_side = (liquidity_usd * 0.5) / max(1e-9, native_price_usd)

        if snapshot.stage in (CurveStage.BONDING, CurveStage.NEAR_GRADUATION):
            # On a bonding curve the effective quote-side depth is the virtual
            # reserve plus whatever real SOL has accumulated, which curve
            # progress tells us directly and is more reliable than the reported
            # liquidity figure aggregators publish for pre-graduation tokens.
            progress = snapshot.bonding_curve_progress
            if progress is None:
                sol_reserve = max(1.0, sol_side) + DEFAULT_VIRTUAL_SOL
            else:
                sol_reserve = DEFAULT_VIRTUAL_SOL + max(0.0, min(85.0, progress * 85.0))
        else:
            sol_reserve = max(1e-6, sol_side)

        # Derive the token reserve from the observed price so the curve's spot
        # price equals the observation exactly. Anchoring depth and price
        # independently produces a curve that disagrees with the market and
        # charges phantom slippage on the very first order, which is what an
        # earlier version of this function did.
        token_reserve = sol_reserve / max(1e-18, price_native)
        return cls(virtual_sol=sol_reserve, virtual_tokens=token_reserve)


@dataclass
class FillContext:
    """Market state and conditions at the moment an order is submitted."""

    snapshot: MarketSnapshot
    curve: CurveState
    native_price_usd: float = 150.0
    #: Price observed one latency-period later, when available. Supplying this
    #: from real forward data is far more honest than modelling drift.
    future_price_native: float | None = None
    #: Recent volatility, used to model adverse drift when no future price exists.
    recent_volatility: float = 0.0
    #: How contested this token is right now, 0..1. Raises failure and sandwich rates.
    contention: float = 0.0


class FillSimulator:
    """Turns an `Order` into a `Fill`, charging for reality."""

    def __init__(self, settings: ExecutionSettings | None = None, seed: int = 1337) -> None:
        self.settings = settings or ExecutionSettings()
        self.rng = random.Random(seed)

    # -- components --------------------------------------------------------- #

    def latency_seconds(self) -> float:
        base = self.settings.base_latency_ms
        jitter = self.rng.gauss(0.0, self.settings.latency_jitter_ms / 2.0)
        return max(0.05, (base + jitter) / 1000.0)

    def _decision_price_drift(self, ctx: FillContext, latency: float) -> float:
        """Multiplicative price change between decision and inclusion.

        When forward data exists we use it, which is the only fully honest
        option. Otherwise we model drift as adverse on average: the reason a
        buy signal fired is usually that price is already moving up, so the
        expected fill is worse than the observed price. Assuming zero drift is
        the optimistic error that makes every momentum backtest look brilliant.
        """
        if ctx.future_price_native and ctx.snapshot.price_native:
            return ctx.future_price_native / max(1e-18, ctx.snapshot.price_native)
        vol = max(0.0, ctx.recent_volatility)
        adverse = 0.5 * vol * math.sqrt(max(0.0, latency))
        noise = self.rng.gauss(0.0, vol * math.sqrt(max(0.0, latency)))
        return max(0.1, 1.0 + adverse + noise)

    def _fails(self, ctx: FillContext) -> bool:
        p = self.settings.fail_probability * (1.0 + 2.0 * ctx.contention)
        return self.rng.random() < min(0.85, p)

    def _sandwiched(self, ctx: FillContext, side: Side) -> bool:
        if side is not Side.BUY:
            return False
        p = self.settings.sandwich_probability * (1.0 + 1.5 * ctx.contention)
        return self.rng.random() < min(0.8, p)

    # -- main --------------------------------------------------------------- #

    def simulate(self, order: Order, ctx: FillContext) -> Fill:
        latency = self.latency_seconds()
        fill_time = order.as_of + timedelta(seconds=latency)

        reference_price = ctx.snapshot.price_native or ctx.curve.spot_price
        if reference_price <= 0:
            return Fill(
                order_client_id=order.client_id,
                token=order.token,
                as_of=fill_time,
                side=order.side,
                amount_token=0.0,
                amount_native=0.0,
                price_native=0.0,
                rejected=True,
                reject_reason="no reference price",
                latency_ms=latency * 1000.0,
            )

        if self._fails(ctx):
            # A failed transaction still burns the fee. Ignoring that flatters
            # any strategy that submits often.
            burned = (order.priority_fee_lamports + order.jito_tip_lamports) / LAMPORTS_PER_SOL
            return Fill(
                order_client_id=order.client_id,
                token=order.token,
                as_of=fill_time,
                side=order.side,
                amount_token=0.0,
                amount_native=0.0,
                price_native=reference_price,
                fee_native=burned,
                rejected=True,
                reject_reason="transaction failed or expired",
                latency_ms=latency * 1000.0,
            )

        drift = self._decision_price_drift(ctx, latency)
        curve = CurveState(
            virtual_sol=ctx.curve.virtual_sol,
            virtual_tokens=ctx.curve.virtual_tokens,
            real_sol=ctx.curve.real_sol,
            real_tokens=ctx.curve.real_tokens,
        )
        # Apply drift by shifting reserves so the curve's spot price matches
        # where the market actually got to while we were in flight.
        if drift != 1.0 and curve.token_reserve > 0:
            curve.real_tokens = max(
                0.0,
                curve.virtual_tokens - (curve.sol_reserve / max(1e-18, reference_price * drift)),
            )

        pre_price = curve.spot_price
        priority_native = order.priority_fee_lamports / LAMPORTS_PER_SOL
        tip_native = order.jito_tip_lamports / LAMPORTS_PER_SOL

        if order.side is Side.BUY:
            gross_native = order.size_native
            platform_fee = gross_native * (self.settings.platform_fee_bps / 10_000.0)
            lp_fee = gross_native * (self.settings.lp_fee_bps / 10_000.0)
            net_in = max(0.0, gross_native - platform_fee - lp_fee)

            if self._sandwiched(ctx, order.side):
                # A front-run pushes our effective entry up before we land.
                penalty = self.settings.sandwich_extra_bps / 10_000.0
                net_in *= 1.0 - penalty

            tokens = curve.tokens_out_for_sol_in(net_in)
            if tokens <= 0:
                return Fill(
                    order_client_id=order.client_id,
                    token=order.token,
                    as_of=fill_time,
                    side=order.side,
                    amount_token=0.0,
                    amount_native=0.0,
                    price_native=pre_price,
                    fee_native=priority_native + tip_native,
                    rejected=True,
                    reject_reason="no depth",
                    latency_ms=latency * 1000.0,
                )

            effective_price = net_in / tokens
            slippage_bps = ((effective_price / max(1e-18, reference_price)) - 1.0) * 10_000.0

            if slippage_bps > order.max_slippage_bps:
                burned = priority_native + tip_native
                return Fill(
                    order_client_id=order.client_id,
                    token=order.token,
                    as_of=fill_time,
                    side=order.side,
                    amount_token=0.0,
                    amount_native=0.0,
                    price_native=effective_price,
                    slippage_bps=slippage_bps,
                    fee_native=burned,
                    rejected=True,
                    reject_reason=(
                        f"slippage {slippage_bps:.0f}bps exceeds limit {order.max_slippage_bps}bps"
                    ),
                    latency_ms=latency * 1000.0,
                )

            return Fill(
                order_client_id=order.client_id,
                token=order.token,
                as_of=fill_time,
                side=order.side,
                amount_token=tokens,
                amount_native=gross_native,
                price_native=effective_price,
                slippage_bps=slippage_bps,
                fee_native=platform_fee + lp_fee + priority_native,
                tip_native=tip_native,
                latency_ms=latency * 1000.0,
            )

        # ---- sell ----------------------------------------------------------
        tokens_in = order.size_native  # for sells, size_native carries token amount
        gross_out = curve.sol_out_for_tokens_in(tokens_in)
        if gross_out <= 0:
            return Fill(
                order_client_id=order.client_id,
                token=order.token,
                as_of=fill_time,
                side=order.side,
                amount_token=0.0,
                amount_native=0.0,
                price_native=pre_price,
                fee_native=priority_native + tip_native,
                rejected=True,
                reject_reason="no exit liquidity",
                latency_ms=latency * 1000.0,
            )

        platform_fee = gross_out * (self.settings.platform_fee_bps / 10_000.0)
        lp_fee = gross_out * (self.settings.lp_fee_bps / 10_000.0)
        net_out = max(0.0, gross_out - platform_fee - lp_fee)
        effective_price = net_out / max(1e-18, tokens_in)
        slippage_bps = (1.0 - (effective_price / max(1e-18, reference_price))) * 10_000.0

        return Fill(
            order_client_id=order.client_id,
            token=order.token,
            as_of=fill_time,
            side=order.side,
            amount_token=tokens_in,
            amount_native=net_out,
            price_native=effective_price,
            slippage_bps=slippage_bps,
            fee_native=platform_fee + lp_fee + priority_native,
            tip_native=tip_native,
            latency_ms=latency * 1000.0,
        )


def estimate_impact_bps(curve: CurveState, sol_in: float) -> float:
    """Price impact in basis points for a given buy size. Used by the exit-depth metric."""
    if sol_in <= 0:
        return 0.0
    before = curve.spot_price
    tokens = curve.tokens_out_for_sol_in(sol_in)
    if tokens <= 0:
        return float("inf")
    effective = sol_in / tokens
    if before <= 0:
        return float("inf")
    return ((effective / before) - 1.0) * 10_000.0


def max_size_within_impact(curve: CurveState, max_bps: float) -> float:
    """Largest buy, in native units, that stays under `max_bps` of impact.

    Closed form for constant product: effective price / spot = 1 + x/X, so the
    largest x satisfying the impact bound is X * max_bps / 10000.
    """
    if max_bps <= 0:
        return 0.0
    return max(0.0, curve.sol_reserve * (max_bps / 10_000.0))


__all__ = [
    "DEFAULT_VIRTUAL_SOL",
    "DEFAULT_VIRTUAL_TOKENS",
    "LAMPORTS_PER_SOL",
    "CurveState",
    "FillContext",
    "FillSimulator",
    "estimate_impact_bps",
    "max_size_within_impact",
]
