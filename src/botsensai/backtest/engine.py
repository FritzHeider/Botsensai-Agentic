"""Event-driven backtester.

The engine replays a token population in wall-clock order, building a
`MetricContext` at each decision point from *only* the data that was both true
and observed by that instant, scoring it, and routing the result through the same
risk manager and the same fill simulator the paper broker uses.

The biases this design is specifically built to avoid, because each of them
inflates memecoin backtests by an order of magnitude:

* **Look-ahead.** Contexts are built through `Database.*_as_of`, which filters on
  `observed_at` as well as `as_of`. Deployer history is time-restricted. Metric
  calibration uses only the training window.
* **Survivorship.** The universe is every launch in the window, including the
  thousands that died in four minutes and are absent from any "top tokens" list.
  A backtest run over tokens that still exist is not a backtest.
* **Fill fantasy.** Every entry pays latency, curve impact, priority fees, tips,
  and a modelled failure and sandwich rate.
* **Exit fantasy.** Peak price is not an exit. Exits execute against the curve
  with the size actually held, which is why `max_realizable_multiple` exists
  alongside `max_multiple_from_t0` in the outcome labels.
* **Overfitting.** Walk-forward with an embargo between train and test, and an
  ablation harness that reports what each metric is actually contributing.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.execution.broker import PaperBroker
from botsensai.metrics import MetricRegistry, build_registry
from botsensai.metrics.base import MetricContext
from botsensai.models import (
    Launch,
    MarketSnapshot,
    Outcome,
    Position,
    Score,
    TokenRef,
    utcnow,
)
from botsensai.scoring.composite import CompositeScorer, Weights, score_to_size
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class TokenTape:
    """Everything known about one token, pre-sorted for replay."""

    launch: Launch
    snapshots: list[MarketSnapshot] = field(default_factory=list)
    trades: list[Any] = field(default_factory=list)
    holders: list[Any] = field(default_factory=list)
    posts: list[Any] = field(default_factory=list)
    security: Any = None
    wallet_priors: dict[str, int] = field(default_factory=dict)
    outcome: Outcome | None = None

    @property
    def token(self) -> TokenRef:
        return self.launch.token

    def snapshot_at(self, when: datetime) -> MarketSnapshot | None:
        """Latest snapshot at or before `when`. Never looks forward."""
        best: MarketSnapshot | None = None
        for s in self.snapshots:
            if s.as_of <= when:
                best = s
            else:
                break
        return best

    def snapshot_after(self, when: datetime) -> MarketSnapshot | None:
        """First snapshot strictly after `when`, used to price latency honestly."""
        for s in self.snapshots:
            if s.as_of > when:
                return s
        return None

    def holders_at(self, when: datetime) -> list[Any]:
        """The single most recent holder slice visible at `when`.

        Holder records are stored as repeated full snapshots, so handing a metric
        every record up to `when` would give it the same wallet several times
        with historical balances. Only the freshest slice is a valid picture of
        who holds what.
        """
        visible = [h for h in self.holders if h.as_of <= when and h.observed_at <= when]
        if not visible:
            return []
        latest = max(h.as_of for h in visible)
        return [h for h in visible if h.as_of == latest]

    def context_at(
        self,
        when: datetime,
        *,
        peer_values: dict[str, list[float]] | None = None,
        recent_narratives: Sequence[str] = (),
        market_regime: dict[str, Any] | None = None,
        deployer_history: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> MetricContext:
        """Build a point-in-time context. This is the anti-look-ahead boundary."""
        return MetricContext(
            token=self.token,
            as_of=when,
            launch=self.launch if self.launch.observed_at <= when else None,
            snapshots=[s for s in self.snapshots if s.as_of <= when and s.observed_at <= when],
            trades=[t for t in self.trades if t.as_of <= when and t.observed_at <= when],
            holders=self.holders_at(when),
            security=(
                self.security
                if self.security is not None
                and self.security.as_of <= when
                and self.security.observed_at <= when
                else None
            ),
            posts=[p for p in self.posts if p.as_of <= when and p.observed_at <= when],
            peer_values=peer_values or {},
            deployer_history=deployer_history or {},
            wallet_priors=self.wallet_priors,
            recent_narratives=list(recent_narratives),
            extra={
                "market_regime": market_regime or {},
                **(extra or {}),
            },
        )


@dataclass
class TradeRecord:
    """One completed round trip, with everything needed to diagnose it."""

    token_key: str
    symbol: str | None
    entered_at: datetime
    exited_at: datetime | None
    score: float
    coverage: float
    regime: str
    size_native: float
    pnl_native: float
    multiple: float
    exit_reason: str
    entry_slippage_bps: float
    hold_seconds: float
    contributions: dict[str, float] = field(default_factory=dict)
    peak_multiple_available: float | None = None


@dataclass
class BacktestResult:
    """Everything a backtest run produced. Serializable for reports."""

    started_at: datetime
    finished_at: datetime
    window_start: datetime
    window_end: datetime
    universe_size: int
    evaluated: int
    entered: int
    trades: list[TradeRecord] = field(default_factory=list)
    account: dict[str, Any] = field(default_factory=dict)
    score_distribution: list[float] = field(default_factory=list)
    veto_counts: dict[str, int] = field(default_factory=dict)
    metric_coverage: dict[str, float] = field(default_factory=dict)
    weights_version: str = "v0"
    synthetic: bool = False
    notes: list[str] = field(default_factory=list)

    # -- performance -------------------------------------------------------- #

    @property
    def total_pnl_native(self) -> float:
        return sum(t.pnl_native for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl_native > 0) / len(self.trades)

    @property
    def median_multiple(self) -> float:
        if not self.trades:
            return 0.0
        return statistics.median(t.multiple for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_native for t in self.trades if t.pnl_native > 0)
        losses = -sum(t.pnl_native for t in self.trades if t.pnl_native < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def expectancy_native(self) -> float:
        if not self.trades:
            return 0.0
        return self.total_pnl_native / len(self.trades)

    def summary(self) -> dict[str, Any]:
        """Headline numbers, with the caveats attached rather than omitted."""
        n = len(self.trades)
        pnl = [t.pnl_native for t in self.trades]
        out: dict[str, Any] = {
            "window": f"{self.window_start.isoformat()} .. {self.window_end.isoformat()}",
            "universe_size": self.universe_size,
            "evaluated": self.evaluated,
            "entered": self.entered,
            "trades": n,
            "total_pnl_native": round(self.total_pnl_native, 6),
            "expectancy_native": round(self.expectancy_native, 6),
            "win_rate": round(self.win_rate, 4),
            "median_multiple": round(self.median_multiple, 4),
            "profit_factor": round(self.profit_factor, 4)
            if math.isfinite(self.profit_factor)
            else "inf",
            "max_drawdown_native": round(self.max_drawdown(), 6),
            "weights_version": self.weights_version,
            "synthetic": self.synthetic,
        }
        if n:
            out["best_trade_native"] = round(max(pnl), 6)
            out["worst_trade_native"] = round(min(pnl), 6)
            out["mean_hold_minutes"] = round(
                statistics.fmean(t.hold_seconds for t in self.trades) / 60.0, 2
            )
        # Honesty flags, not decoration: a headline number from 6 trades means
        # nothing, and a report that does not say so is misleading.
        if n < 30:
            out["warning"] = (
                f"only {n} trades; this is far below the sample needed for any "
                "conclusion in a fat-tailed return distribution"
            )
        if self.synthetic:
            out["warning_synthetic"] = (
                "run over synthetic data — proves the pipeline executes, proves "
                "nothing whatsoever about profitability"
            )
        out["notes"] = self.notes
        return out

    def max_drawdown(self) -> float:
        """Peak-to-trough of the cumulative PnL curve, in native units."""
        if not self.trades:
            return 0.0
        equity = 0.0
        peak = 0.0
        worst = 0.0
        for t in sorted(self.trades, key=lambda x: x.entered_at):
            equity += t.pnl_native
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst

    def bootstrap_expectancy_ci(
        self, samples: int = 2000, seed: int = 1337, alpha: float = 0.05
    ) -> tuple[float, float]:
        """Bootstrap confidence interval on expectancy.

        Reported instead of a Sharpe ratio, which assumes a distribution this
        market does not have. With returns this skewed, the interval is usually
        wide enough to make clear that an apparently strong result is not yet
        distinguishable from noise — which is the point.
        """
        import random as _random

        pnl = [t.pnl_native for t in self.trades]
        if len(pnl) < 5:
            return (0.0, 0.0)
        rng = _random.Random(seed)
        means: list[float] = []
        n = len(pnl)
        for _ in range(samples):
            resample = [pnl[rng.randrange(n)] for _ in range(n)]
            means.append(sum(resample) / n)
        means.sort()
        lo = means[int(alpha / 2 * samples)]
        hi = means[int((1 - alpha / 2) * samples)]
        return (round(lo, 6), round(hi, 6))


class Backtester:
    """Replays a token population and produces a `BacktestResult`."""

    def __init__(
        self,
        settings: Settings | None = None,
        registry: MetricRegistry | None = None,
        weights: Weights | None = None,
        decision_interval_seconds: float = 60.0,
        max_decision_age_seconds: float = 3600.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or build_registry()
        self.scorer = CompositeScorer(self.registry, weights, self.settings)
        self.decision_interval = decision_interval_seconds
        self.max_decision_age = max_decision_age_seconds

    # -- universe construction ---------------------------------------------- #

    @staticmethod
    def tapes_from_database(
        db: Database, start: datetime, end: datetime
    ) -> list[TokenTape]:
        """Load a replayable universe from persisted collection data."""
        tapes: list[TokenTape] = []
        for launch in db.launches_between(start, end):
            key = launch.token.key
            horizon = end
            tape = TokenTape(
                launch=launch,
                snapshots=db.snapshots_as_of(key, horizon),
                trades=db.trades_as_of(key, horizon),
                holders=db.holders_as_of(key, horizon),
                posts=db.posts_as_of(key, horizon),
                security=db.security_as_of(key, horizon),
                outcome=db.outcome(key),
            )
            # Prior trading history per wallet, restricted to before this launch.
            priors: dict[str, int] = {}
            for t in tape.trades:
                if t.wallet not in priors:
                    priors[t.wallet] = db.wallet_seen_before(t.wallet, launch.created_at)
            tape.wallet_priors = priors
            tapes.append(tape)
        return tapes

    @staticmethod
    def tapes_from_synthetic(tokens: Iterable[Any]) -> list[TokenTape]:
        """Wrap `SyntheticToken` objects as tapes."""
        out: list[TokenTape] = []
        for t in tokens:
            out.append(
                TokenTape(
                    launch=t.launch,
                    snapshots=sorted(t.snapshots, key=lambda s: s.as_of),
                    trades=sorted(t.trades, key=lambda x: x.as_of),
                    holders=t.holders,
                    posts=sorted(t.posts, key=lambda p: p.as_of),
                    security=t.security,
                    wallet_priors=t.wallet_priors,
                )
            )
        return out

    # -- regime and peers --------------------------------------------------- #

    @staticmethod
    def compute_regime(tapes: Sequence[TokenTape], when: datetime, lookback_hours: float = 24.0) -> dict[str, Any]:
        """Market-wide state as of `when`, from launches already visible."""
        cutoff = when - timedelta(hours=lookback_hours)
        recent = [t for t in tapes if cutoff <= t.launch.created_at <= when]
        if not recent:
            return {}
        graduated = 0
        for t in recent:
            snap = t.snapshot_at(when)
            if snap is not None and (snap.bonding_curve_progress or 0.0) >= 0.95:
                graduated += 1
        hours = max(1.0, lookback_hours)
        return {
            "graduation_rate_24h": graduated / len(recent),
            "launches_per_hour": len(recent) / hours,
            "new_token_inflow_usd_1h": sum(
                snap.liquidity_usd or 0.0
                for snap in (t.snapshot_at(when) for t in recent)
                if snap is not None
            )
            / hours,
            "sample_size": len(recent),
        }

    @staticmethod
    def recent_narratives(tapes: Sequence[TokenTape], when: datetime, hours: float = 6.0) -> list[str]:
        cutoff = when - timedelta(hours=hours)
        out: list[str] = []
        for t in tapes:
            if cutoff <= t.launch.created_at < when:
                parts = [t.launch.token.name or "", t.launch.token.symbol or "", t.launch.description or ""]
                joined = " ".join(p for p in parts if p).strip()
                if joined:
                    out.append(joined)
        return out

    # -- main loop ---------------------------------------------------------- #

    def run(
        self,
        tapes: Sequence[TokenTape],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        starting_native: float = 10.0,
        synthetic: bool = False,
        on_score: Callable[[TokenTape, Score], None] | None = None,
    ) -> BacktestResult:
        run_start = utcnow()
        if not tapes:
            return BacktestResult(
                started_at=run_start,
                finished_at=utcnow(),
                window_start=start or run_start,
                window_end=end or run_start,
                universe_size=0,
                evaluated=0,
                entered=0,
                synthetic=synthetic,
                notes=["empty universe"],
            )

        window_start = start or min(t.launch.created_at for t in tapes)
        window_end = end or max(
            (t.snapshots[-1].as_of if t.snapshots else t.launch.created_at) for t in tapes
        )

        broker = PaperBroker(self.settings, starting_native=starting_native, seed=self.settings.seed)
        by_key = {t.token.key: t for t in tapes}

        # Build the event schedule: one decision point per token per interval,
        # bounded by the maximum age we are willing to enter at.
        events: list[tuple[datetime, str]] = []
        for tape in tapes:
            t0 = tape.launch.created_at
            horizon = min(
                t0 + timedelta(seconds=self.max_decision_age),
                tape.snapshots[-1].as_of if tape.snapshots else t0,
                window_end,
            )
            step = self.decision_interval
            n_steps = int(max(0.0, (horizon - t0).total_seconds()) // step)
            for i in range(1, n_steps + 1):
                events.append((t0 + timedelta(seconds=i * step), tape.token.key))
        events.sort(key=lambda e: e[0])

        scores_seen: list[float] = []
        veto_counts: dict[str, int] = {}
        metric_hits: dict[str, int] = {}
        metric_total = 0
        evaluated = 0
        entered = 0
        entry_info: dict[str, tuple[Score, float]] = {}
        trades: list[TradeRecord] = []

        # Peer values accumulate as the replay proceeds, so cross-sectional
        # normalization only ever uses metrics already computed on earlier
        # tokens. Seeding it from the whole population would be look-ahead.
        peer_values: dict[str, list[float]] = {}

        last_regime_at: datetime | None = None
        regime_cache: dict[str, Any] = {}

        for when, key in events:
            tape = by_key[key]
            snapshot = tape.snapshot_at(when)
            if snapshot is None:
                continue

            broker.mark(tape.token, snapshot.price_native or 0.0)

            # Exits first: a position must be able to close before new capital
            # is committed, otherwise the exposure cap is enforced against a
            # stale portfolio.
            for fill in broker.apply_exits(tape.token, snapshot, when):
                if fill.rejected:
                    continue

            if key in entry_info and key not in broker.account.positions:
                score, size = entry_info.pop(key)
                closed = next(
                    (p for p in reversed(broker.account.closed) if p.token.key == key), None
                )
                if closed is not None:
                    trades.append(self._record(tape, score, size, closed))

            if (
                last_regime_at is None
                or (when - last_regime_at).total_seconds() >= 900
            ):
                regime_cache = self.compute_regime(tapes, when)
                last_regime_at = when

            already_in = key in broker.account.positions
            age = (when - tape.launch.created_at).total_seconds()

            ctx = tape.context_at(
                when,
                peer_values=peer_values,
                recent_narratives=self.recent_narratives(tapes, when),
                market_regime=regime_cache,
                deployer_history=self._deployer_history(tapes, tape, when),
                extra={
                    "target_position_usd": self.settings.risk.max_position_native * 150.0,
                    "max_impact_pct": self.settings.risk.max_slippage_bps / 10_000.0,
                },
            )

            values = self.registry.evaluate_all(ctx)
            score = self.scorer.score(ctx, values)
            evaluated += 1
            scores_seen.append(score.composite)

            for value in values:
                metric_total += 1
                if value.usable:
                    metric_hits[value.metric_id] = metric_hits.get(value.metric_id, 0) + 1
                if value.raw is not None:
                    peer_values.setdefault(value.metric_id, []).append(value.raw)

            for veto in score.vetoes:
                veto_counts[veto.value] = veto_counts.get(veto.value, 0) + 1

            if on_score is not None:
                on_score(tape, score)

            if already_in:
                should_exit, reason = self.scorer.should_exit(score)
                if should_exit:
                    broker.close_position(tape.token, snapshot, when, reason=reason)
                continue

            ok, _reason = self.scorer.should_enter(score)
            if not ok:
                continue

            size = score_to_size(
                score,
                self.settings.risk.max_position_native,
                stop_loss_fraction=self.settings.risk.stop_loss_pct,
            )
            if size <= 1e-6:
                continue

            forward = tape.snapshot_after(when)
            entry_fill = broker.open_position(
                tape.token,
                size,
                snapshot,
                when,
                age_seconds=age,
                reason=score.explanation or "",
                score=score.composite,
                contention=self._contention(ctx),
                future_price_native=forward.price_native if forward else None,
                recent_volatility=self._volatility(tape, when),
            )
            if entry_fill is not None and not entry_fill.rejected:
                entered += 1
                entry_info[key] = (score, size)

        # Liquidate anything still open at the end of the window.
        final_snapshots = {
            k: (by_key[k].snapshots[-1] if by_key[k].snapshots else None)
            for k in list(broker.account.positions.keys())
        }
        for key, snapshot in final_snapshots.items():
            if snapshot is None:
                continue
            broker.close_position(
                by_key[key].token, snapshot, window_end, reason="end of backtest window"
            )
            if key in entry_info:
                score, size = entry_info.pop(key)
                closed = next(
                    (p for p in reversed(broker.account.closed) if p.token.key == key), None
                )
                if closed is not None:
                    trades.append(self._record(by_key[key], score, size, closed))

        coverage = {
            mid: round(hits / max(1, evaluated), 4) for mid, hits in sorted(metric_hits.items())
        }
        for metric in self.registry:
            coverage.setdefault(metric.id, 0.0)

        notes: list[str] = []
        thin = [mid for mid, c in coverage.items() if c < 0.2]
        if thin:
            notes.append(
                f"{len(thin)} metrics produced usable values in under 20% of evaluations: "
                + ", ".join(sorted(thin)[:8])
                + ("..." if len(thin) > 8 else "")
            )
        if entered and entered < 20:
            notes.append(
                f"only {entered} entries were taken; thresholds may be too strict for this universe"
            )

        return BacktestResult(
            started_at=run_start,
            finished_at=utcnow(),
            window_start=window_start,
            window_end=window_end,
            universe_size=len(tapes),
            evaluated=evaluated,
            entered=entered,
            trades=trades,
            account=broker.summary(),
            score_distribution=scores_seen,
            veto_counts=veto_counts,
            metric_coverage=coverage,
            weights_version=self.scorer.weights.version,
            synthetic=synthetic,
            notes=notes,
        )

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _record(tape: TokenTape, score: Score, size: float, position: Position) -> TradeRecord:
        entry_fill = next((f for f in position.fills if f.side.value == "buy"), None)
        hold = (
            (position.closed_at - position.opened_at).total_seconds()
            if position.closed_at
            else 0.0
        )
        cost = sum(f.amount_native for f in position.fills if f.side.value == "buy")
        proceeds = sum(f.amount_native for f in position.fills if f.side.value == "sell")
        multiple = proceeds / cost if cost > 0 else 0.0
        return TradeRecord(
            token_key=tape.token.key,
            symbol=tape.token.symbol,
            entered_at=position.opened_at,
            exited_at=position.closed_at,
            score=score.composite,
            coverage=score.coverage,
            regime=score.regime,
            size_native=size,
            pnl_native=position.realized_pnl_native,
            multiple=multiple,
            exit_reason=position.exit_reason or "",
            entry_slippage_bps=entry_fill.slippage_bps if entry_fill else 0.0,
            hold_seconds=hold,
            contributions=dict(score.contributions),
        )

    @staticmethod
    def _deployer_history(
        tapes: Sequence[TokenTape], tape: TokenTape, when: datetime
    ) -> dict[str, Any]:
        """Prior record for this deployer, using only launches created earlier.

        Rebuilt per decision point rather than cached, because a deployer's
        record changes during the replay and freezing it would import the
        future.
        """
        deployer = tape.launch.deployer
        if not deployer:
            return {}
        priors = [
            t
            for t in tapes
            if t.launch.deployer == deployer and t.launch.created_at < tape.launch.created_at
        ]
        if not priors:
            return {"launch_count": 0, "rug_count": 0, "graduate_count": 0, "best_multiple": 0.0}
        rugs = sum(1 for t in priors if t.outcome is not None and t.outcome.rugged)
        grads = sum(1 for t in priors if t.outcome is not None and t.outcome.graduated)
        best = max(
            (t.outcome.max_multiple_from_t0 or 0.0) for t in priors if t.outcome is not None
        ) if any(t.outcome for t in priors) else 0.0
        return {
            "launch_count": len(priors),
            "rug_count": rugs,
            "graduate_count": grads,
            "best_multiple": best,
        }

    @staticmethod
    def _contention(ctx: MetricContext) -> float:
        """0..1 measure of how contested entry is right now, feeding the fill model."""
        fees = [
            float((t.priority_fee_lamports or 0) + (t.jito_tip_lamports or 0))
            for t in ctx.trades_within(300.0)
        ]
        if not fees:
            return 0.0
        median = statistics.median(fees)
        return min(1.0, math.log10(max(1.0, median) / 200_000.0) / 1.5) if median > 0 else 0.0

    @staticmethod
    def _volatility(tape: TokenTape, when: datetime, lookback_seconds: float = 300.0) -> float:
        """Realized log-return volatility per second over the recent window."""
        cutoff = when - timedelta(seconds=lookback_seconds)
        prices = [
            s.price_native
            for s in tape.snapshots
            if cutoff <= s.as_of <= when and s.price_native and s.price_native > 0
        ]
        if len(prices) < 3:
            return 0.0
        returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
        if len(returns) < 2:
            return 0.0
        step = lookback_seconds / max(1, len(returns))
        return statistics.pstdev(returns) / math.sqrt(max(1.0, step))


__all__ = ["BacktestResult", "Backtester", "TokenTape", "TradeRecord"]
