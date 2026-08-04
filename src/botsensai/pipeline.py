"""The live loop: discover, screen, enrich, score, decide, remember.

The ordering here is the whole design. Every surface has a rate limit, several
are severely constrained, and the candidate population is thousands of tokens an
hour. Spending the expensive budgets uniformly across that population exhausts
them in seconds and starves the handful of tokens that actually deserved
attention.

So the loop is a funnel:

1. **Discover** — cheap, broad. Everything that launched recently, from every
   venue that will tell us, plus whatever is being actively promoted.
2. **Screen** — free. Pure local filtering on facts we already have: age,
   liquidity floor, description substance, ticker collisions. This removes the
   overwhelming majority of the feed at zero API cost.
3. **Enrich** — expensive, narrow. Trades, holders, security and social, spent
   only on survivors of the screen, in rank order, until the budget runs out.
4. **Score** — free. All 32 metrics, the veto gates, the composite.
5. **Decide** — size and route through the risk manager and the broker.
6. **Remember** — write what happened back into agentic memory, so the next
   sweep starts from a system that has learned something.

Step 6 is what makes this an agent rather than a script. The bot records the
regimes it observes, the heuristics it forms, and the post-mortems of its own
trades, then reads them back as inputs on the next pass.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from botsensai.collectors.base import CollectionResult, Collector, CollectorRegistry
from botsensai.collectors.dexscreener import DexscreenerCollector
from botsensai.collectors.geckoterminal import GeckoTerminalCollector
from botsensai.collectors.pumpfun import PumpFunCollector
from botsensai.collectors.social import (
    FourChanBizCollector,
    RedditCollector,
    TelegramChannelCollector,
    XCollector,
)
from botsensai.collectors.x_session import AuthenticatedXCollector
from botsensai.config import Settings, get_settings
from botsensai.execution.broker import PaperBroker
from botsensai.media.hasher import MediaHasher
from botsensai.memory.store import MemoryStore
from botsensai.metrics import MetricRegistry, build_registry
from botsensai.metrics.base import MetricContext
from botsensai.models import (
    HolderRecord,
    Launch,
    MemoryKind,
    Score,
    utcnow,
)
from botsensai.onchain.funding import (
    DEFAULT_RESOLVE_DEADLINE_SECONDS,
    DEFAULT_RESOLVE_LIMIT,
    FundingIndex,
    FundingSourceResolver,
)
from botsensai.onchain.wallet_priors import WalletPriorIndex
from botsensai.scoring.composite import CompositeScorer, score_to_size
from botsensai.store.db import Database
from botsensai.util.logging import get_logger
from botsensai.util.text import tokens as text_tokens

log = get_logger(__name__)

#: Surface name used for the per-sweep heartbeat row in `collector_runs`.
#: It is not a data surface. It is written once per sweep, whatever happened, so
#: that a stretch of time with no row in it is provably a stretch of time when
#: nothing was collecting — which is the only way to tell a quiet market from a
#: daemon that died at 3am.
HEARTBEAT_SURFACE = "sweep"

#: A sweep is allowed to overrun the collection deadline by this much before it
#: is cut off. Without a cap, `collect --hours N` returns whenever the last
#: sweep happens to finish, which makes it unusable under an external timeout.
DEADLINE_GRACE_SECONDS = 15.0

#: How many sweep reports a session keeps. A day of one-minute sweeps is 1440
#: reports; a week is ten thousand. The counters below are exact regardless.
MAX_RETAINED_REPORTS = 200


@dataclass
class SweepReport:
    """What one pass of the loop did."""

    started_at: datetime
    finished_at: datetime | None = None
    discovered: int = 0
    screened_in: int = 0
    enriched: int = 0
    scored: int = 0
    entered: int = 0
    exited: int = 0
    degraded_surfaces: list[str] = field(default_factory=list)
    top_candidates: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    regime: str = "unknown"

    def summary(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(
                ((self.finished_at or utcnow()) - self.started_at).total_seconds(), 2
            ),
            "discovered": self.discovered,
            "screened_in": self.screened_in,
            "enriched": self.enriched,
            "scored": self.scored,
            "entered": self.entered,
            "exited": self.exited,
            "regime": self.regime,
            "degraded_surfaces": self.degraded_surfaces,
            "errors": self.errors[:5],
            "top_candidates": self.top_candidates[:10],
        }


@dataclass
class CollectionSession:
    """What a whole `collect` run did.

    The counters are exact totals over every sweep; `recent` holds only the last
    `MAX_RETAINED_REPORTS` reports, because a daemon that runs for a week must
    not accumulate a week of reports in memory to be able to print a summary.
    """

    started_at: datetime
    finished_at: datetime | None = None
    stopped_because: str = "deadline"
    sweeps: int = 0
    failed_sweeps: int = 0
    discovered: int = 0
    scored: int = 0
    entered: int = 0
    exited: int = 0
    degraded_surfaces: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    recent: list[SweepReport] = field(default_factory=list)

    def record(self, report: SweepReport) -> None:
        self.sweeps += 1
        self.discovered += report.discovered
        self.scored += report.scored
        self.entered += report.entered
        self.exited += report.exited
        if report.errors:
            self.failed_sweeps += 1
        for surface in report.degraded_surfaces:
            self.degraded_surfaces[surface] = self.degraded_surfaces.get(surface, 0) + 1
        for error in report.errors:
            self.errors.append(error)
        del self.errors[:-MAX_RETAINED_REPORTS]
        self.recent.append(report)
        del self.recent[:-MAX_RETAINED_REPORTS]

    def summary(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(
                ((self.finished_at or utcnow()) - self.started_at).total_seconds(), 1
            ),
            "stopped_because": self.stopped_because,
            "sweeps": self.sweeps,
            "failed_sweeps": self.failed_sweeps,
            "discovered": self.discovered,
            "scored": self.scored,
            "entered": self.entered,
            "exited": self.exited,
            "degraded_surfaces": dict(self.degraded_surfaces),
        }


class Pipeline:
    """Orchestrates one sweep, or many."""

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        memory: MemoryStore | None = None,
        registry: MetricRegistry | None = None,
        broker: PaperBroker | None = None,
        collectors: Sequence[Collector] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or Database(self.settings.path(self.settings.db_path))
        self.memory = (
            memory
            if memory is not None
            else MemoryStore(
                self.settings.path(self.settings.memory.path),
                decay_half_life_hours=self.settings.memory.decay_half_life_hours,
                min_confidence=self.settings.memory.min_confidence_to_apply,
            )
        )
        self.metrics = registry or build_registry()
        self.scorer = CompositeScorer(self.metrics, None, self.settings)
        self.broker = broker or PaperBroker(self.settings, starting_native=10.0)
        self.wallet_priors = WalletPriorIndex(self.db)
        # Funding is resolved over RPC, so the resolver is attached only when
        # that surface is enabled. Without it the index still serves whatever is
        # already cached, which is what a backtest needs and what an offline run
        # gets: reads never depend on the network.
        rpc = self.settings.collector(FundingSourceResolver.surface)
        self.funding = FundingIndex(
            self.db,
            self.settings,
            resolver=FundingSourceResolver(self.settings) if rpc.enabled else None,
        )

        # Media hashing sits in the pipeline rather than in each social collector
        # so the byte budget, the URL cache and the deadline are shared across
        # every platform in a sweep instead of being re-spent per surface.
        self.media_hasher = MediaHasher(self.settings)

        self.collectors = CollectorRegistry(self.settings)
        for collector in collectors or self._default_collectors():
            self.collectors.register(collector)

        self._active_themes: list[tuple[str, float]] = []
        self._market_regime: dict[str, Any] = {}
        self._recent_narratives: list[str] = []
        self._ticker_index: dict[str, list[dict[str, Any]]] = {}
        # Per-token evidence that arrives as a raw collector payload rather than
        # as a stored record, e.g. X's fast-follower classification.
        self._fast_follower_share: dict[str, float] = {}
        #: The most recent (or in-flight) collection session, for callers that
        #: need to report on a run that was interrupted rather than returned.
        self.last_session: CollectionSession | None = None

    def _default_collectors(self) -> list[Collector]:
        """Market surfaces first, then social.

        Order matters for the social ones only in that they are cheap to skip:
        each is independently degradable, and the metric layer lowers confidence
        rather than treating a dark surface as a bearish reading.
        """
        return [
            PumpFunCollector(self.settings),
            DexscreenerCollector(self.settings),
            GeckoTerminalCollector(self.settings),
            # Both register under the surface name "x", so exactly one is active.
            # The authenticated one degrades to the public path internally when
            # its session turns out not to be valid, so choosing it here is safe
            # even if the profile has since been logged out.
            (
                AuthenticatedXCollector(self.settings)
                if self.settings.x_session_enabled
                else XCollector(self.settings)
            ),
            RedditCollector(self.settings),
            FourChanBizCollector(self.settings),
            TelegramChannelCollector(self.settings),
        ]

    async def aclose(self) -> None:
        await self.collectors.aclose()
        await self.media_hasher.aclose()
        if self.funding.resolver is not None:
            await self.funding.resolver.aclose()

    # -- step 1: discover --------------------------------------------------- #

    async def discover(self, limit: int = 60) -> CollectionResult:
        results = await self.collectors.sweep_discover(limit)
        combined = CollectorRegistry.combine(results, surface="discover")

        for launch in combined.launches:
            self.db.upsert_launch(launch)
        self.db.insert_snapshots(combined.snapshots)

        # Harvest the cross-cutting context the metrics need from raw payloads.
        metas = combined.raw.get("metas")
        if metas:
            self._active_themes = DexscreenerCollector.active_themes(metas)

        self._rebuild_narrative_context()
        return combined

    def _rebuild_narrative_context(self, hours: float = 6.0) -> None:
        """Refresh the recent-launch corpus and ticker collision index.

        Both are cross-sectional: a token's novelty and its ticker contention are
        properties of the population it launched into, so they have to be
        recomputed from the population rather than looked up per token.
        """
        now = utcnow()
        recent = self.db.launches_between(now - timedelta(hours=hours), now)
        narratives: list[str] = []
        index: dict[str, list[dict[str, Any]]] = {}
        for launch in recent:
            parts = [
                launch.token.name or "",
                launch.token.symbol or "",
                launch.description or "",
            ]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                narratives.append(joined)
            symbol = (launch.token.symbol or "").upper().strip()
            if symbol:
                snapshots = self.db.snapshots_as_of(launch.token.key, now)
                liquidity = snapshots[-1].liquidity_usd if snapshots else None
                index.setdefault(symbol, []).append(
                    {"token_key": launch.token.key, "liquidity_usd": liquidity}
                )
        self._recent_narratives = narratives
        self._ticker_index = index

    # -- step 2: screen ----------------------------------------------------- #

    def screen(self, launches: Sequence[Launch], max_candidates: int = 40) -> list[Launch]:
        """Free local filtering. The step that makes the rate limits survivable.

        Ranking uses only facts already in hand: age inside the tradeable window,
        whether there is any liquidity at all, whether the launch shows human
        effort, and whether its ticker is contested. Nothing here costs an API
        call, and it typically removes well over ninety percent of the feed.
        """
        now = utcnow()
        risk = self.settings.risk
        scored: list[tuple[float, Launch]] = []

        for launch in launches:
            age = (now - launch.created_at).total_seconds()
            if age < risk.min_token_age_seconds or age > risk.max_token_age_seconds:
                continue

            snapshots = self.db.snapshots_as_of(launch.token.key, now)
            latest = snapshots[-1] if snapshots else None
            if latest is not None and latest.liquidity_usd is not None:
                if latest.liquidity_usd < risk.min_liquidity_usd * 0.5:
                    continue

            rank = 0.0
            # Human effort at launch time: description length, socials, image.
            description_words = len(text_tokens(launch.description or ""))
            rank += min(1.0, description_words / 20.0) * 0.3
            rank += 0.2 * sum(
                1 for s in (launch.twitter, launch.telegram, launch.website) if s
            ) / 3.0
            rank += 0.1 if launch.image_uri else 0.0

            # Ticker contention is a penalty, and it is free to compute.
            symbol = (launch.token.symbol or "").upper().strip()
            collisions = len(self._ticker_index.get(symbol, [])) - 1
            rank -= min(0.3, max(0, collisions) * 0.05)

            # Freshness: earlier in the window is worth more, all else equal.
            rank += 0.3 * max(0.0, 1.0 - age / max(1.0, risk.max_token_age_seconds))

            if latest is not None and latest.volume_5m_usd:
                rank += min(0.2, latest.volume_5m_usd / 50_000.0)

            scored.append((rank, launch))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [launch for _, launch in scored[:max_candidates]]

    # -- step 3: enrich ----------------------------------------------------- #

    async def enrich(self, launches: Sequence[Launch]) -> CollectionResult:
        # The social collectors need to know which account and channel belong to
        # which token, and only the launch metadata knows that. Wiring it here
        # keeps the collectors free of any dependency on the store.
        self._wire_social_handles(launches)

        tokens = [launch.token for launch in launches]
        results = await self.collectors.sweep_enrich(tokens)

        # Record each surface separately before combining. `combine` folds
        # everything into one result, which is what made a single surface being
        # killed by its timeout invisible: the sweep reported "degraded: enrich"
        # and never said which surface, or that it had produced nothing.
        run_id = uuid.uuid4().hex
        for outcome in results:
            self.db.record_run(
                run_id=run_id,
                surface=outcome.surface,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                ok=outcome.ok and not outcome.degraded,
                records=outcome.record_count,
                error=outcome.error,
            )

        combined = CollectorRegistry.combine(results, surface="enrich")

        self.db.insert_snapshots(combined.snapshots)
        self.db.insert_trades(combined.trades)
        self.db.insert_holders(combined.holders)
        # Fold the wallets we just learned about into the profile summary, so the
        # next sweep can answer "this wallet is younger than the token" without a
        # counting query. Scoped to the wallets in hand — a full rebuild is a
        # corpus-wide scan and this runs every sweep.
        if combined.trades:
            self.wallet_priors.refresh_profiles({t.wallet for t in combined.trades})
        await self._resolve_funding(combined.holders)
        for report in combined.security:
            self.db.insert_security(report)
        # Each post carries the token it was collected for, so this must be a
        # single call that lets `insert_posts` read `post.token_key`. Passing no
        # key at all — which this did — wrote every post with token_key NULL,
        # and `posts_as_of` filters on that column: 494 posts were stored and
        # none was ever readable by a social metric.
        # Hashes must be attached before the write, not after: `insert_posts`
        # is the only path into the store and a second pass would have to update
        # rows it has no key to find.
        await self.media_hasher.hash_posts(combined.posts)
        self.db.insert_posts(combined.posts)

        fast = combined.raw.get("fast_follower_share")
        if isinstance(fast, dict):
            self._fast_follower_share.update(fast)
        return combined

    async def _resolve_funding(self, holders: Sequence[HolderRecord]) -> int:
        """Look up the funders we do not have yet, largest holdings first.

        Ordering is the whole budget decision. Two RPC calls per wallet against
        an endpoint that 429s at ten a second means a sweep can afford a few
        dozen lookups, and the wallets that decide whether a distribution is one
        actor or forty are the ones at the top of the holder table, not the
        dust at the bottom.
        """
        if not holders:
            return 0
        ranked = sorted(holders, key=lambda h: h.share_of_supply, reverse=True)
        wallets = list(dict.fromkeys(h.wallet for h in ranked))
        rpc = self.settings.collector(FundingSourceResolver.surface)
        written = await self.funding.resolve_missing(
            wallets,
            limit=int(rpc.extra.get("max_funding_resolutions", DEFAULT_RESOLVE_LIMIT)),
            deadline_seconds=float(
                rpc.extra.get("funding_deadline_seconds", DEFAULT_RESOLVE_DEADLINE_SECONDS)
            ),
        )
        if written:
            log.info("pipeline.funding_resolved", wallets=written, candidates=len(wallets))
        return written

    def _wire_social_handles(self, launches: Sequence[Launch]) -> None:
        """Map token -> X handle and Telegram channel from launch metadata."""
        handles: dict[str, str] = {}
        channels: dict[str, str] = {}
        for launch in launches:
            if launch.twitter:
                handle = launch.twitter.rstrip("/").split("/")[-1].lstrip("@")
                if handle and "?" not in handle:
                    handles[launch.token.key] = handle
            if launch.telegram:
                channel = launch.telegram.rstrip("/").split("/")[-1]
                if channel and "+" not in channel and "joinchat" not in channel:
                    channels[launch.token.key] = channel

        x_collector = self.collectors.get("x")
        if x_collector is not None:
            x_collector.config.extra["handles"] = handles
        telegram = self.collectors.get("telegram")
        if telegram is not None:
            telegram.config.extra.setdefault("call_channels", [])
            telegram.config.extra["channels"] = channels

    # -- step 4: score ------------------------------------------------------ #

    def build_context(self, launch: Launch, as_of: datetime | None = None) -> MetricContext:
        """Assemble a point-in-time context from the store.

        Uses the same `*_as_of` reads the backtester uses, so a context built
        live and a context built in replay are constructed identically. That
        symmetry is the only reason a backtest result says anything about live
        behaviour.
        """
        when = as_of or utcnow()
        key = launch.token.key
        symbol = (launch.token.symbol or "").upper().strip()
        collisions = [
            entry
            for entry in self._ticker_index.get(symbol, [])
            if entry["token_key"] != key
        ]

        trades = self.db.trades_as_of(key, when)
        # Prior history is bounded twice: trades that happened before this token
        # existed (`launch.created_at`) and that we had already collected by the
        # instant being scored (`when`). Dropping either bound is look-ahead.
        priors = self.wallet_priors.priors_for(
            (t.wallet for t in trades), launch.created_at, observed_before=when
        )

        return MetricContext(
            token=launch.token,
            as_of=when,
            launch=launch,
            snapshots=self.db.snapshots_as_of(key, when),
            trades=trades,
            # Funders are attached here rather than stored on the holder row:
            # the answer is per wallet, not per (token, wallet, slice), so one
            # cached lookup serves every token that wallet ever holds. Bounded
            # on funding event time, and exchange withdrawals are withheld so
            # unrelated customers of one exchange do not read as one cluster.
            holders=self.funding.apply(self.db.holders_as_of(key, when), before=when),
            security=self.db.security_as_of(key, when),
            posts=self.db.posts_as_of(key, when),
            deployer_history=(
                self.db.deployer_history(launch.deployer, before=launch.created_at)
                if launch.deployer
                else {}
            ),
            wallet_priors=priors,
            recent_narratives=self._recent_narratives,
            degraded_surfaces=set(),
            extra={
                "market_regime": self._market_regime,
                "active_themes": self._active_themes,
                "ticker_collisions": collisions,
                "fast_follower_share": self._fast_follower_share,
                "target_position_usd": self.settings.risk.max_position_native * 150.0,
                "max_impact_pct": self.settings.risk.max_slippage_bps / 10_000.0,
            },
        )

    def score(self, launch: Launch, as_of: datetime | None = None) -> Score:
        ctx = self.build_context(launch, as_of)
        result = self.scorer.score(ctx)
        self.db.insert_metric_values(result.metric_values)
        self.db.insert_score(result)
        return result

    def compute_regime(self, hours: float = 24.0) -> dict[str, Any]:
        """Market-wide state, recomputed from the store each sweep."""
        now = utcnow()
        recent = self.db.launches_between(now - timedelta(hours=hours), now)
        if not recent:
            return {}
        graduated = 0
        inflow = 0.0
        for launch in recent:
            snapshots = self.db.snapshots_as_of(launch.token.key, now)
            if not snapshots:
                continue
            latest = snapshots[-1]
            if (latest.bonding_curve_progress or 0.0) >= 0.95 or latest.stage.value == "graduated":
                graduated += 1
            inflow += latest.liquidity_usd or 0.0
        regime = {
            "graduation_rate_24h": graduated / len(recent),
            "launches_per_hour": len(recent) / max(1.0, hours),
            "new_token_inflow_usd_1h": inflow / max(1.0, hours),
            "sample_size": len(recent),
        }
        self._market_regime = regime
        return regime

    # -- step 5 and 6: decide and remember ---------------------------------- #

    def decide(self, launch: Launch, result: Score) -> str:
        """Route a score through risk and the broker. Returns what happened."""
        ok, reason = self.scorer.should_enter(result)
        if not ok:
            return f"skip: {reason}"

        snapshots = self.db.snapshots_as_of(launch.token.key, result.as_of)
        if not snapshots:
            return "skip: no market snapshot"
        latest = snapshots[-1]

        size = score_to_size(
            result,
            self.settings.risk.max_position_native,
            stop_loss_fraction=self.settings.risk.stop_loss_pct,
        )
        if size <= 1e-6:
            return "skip: sizing produced zero"

        age = (result.as_of - launch.created_at).total_seconds()
        fill = self.broker.open_position(
            launch.token,
            size,
            latest,
            result.as_of,
            age_seconds=age,
            reason=result.explanation or "",
            score=result.composite,
        )
        if fill is None:
            return "skip: rejected by risk manager"
        if fill.rejected:
            return f"failed: {fill.reject_reason}"

        self.memory.remember(
            MemoryKind.OBSERVATION,
            subject=launch.token.key,
            title=f"Entered {launch.token.symbol or launch.token.mint[:8]} at {result.composite:.3f}",
            body=result.explanation or "",
            tags=["entry", result.regime],
            confidence=result.composite,
            evidence=[f"{k}={v:.3f}" for k, v in list(result.contributions.items())[:6]],
        )
        return f"entered {size:.4f} native at {fill.price_native:.3e}"

    def write_regime_memory(self, regime: dict[str, Any]) -> None:
        """Record the market regime so later sessions can condition on it."""
        if not regime or regime.get("sample_size", 0) < 20:
            return
        label = CompositeScorer.classify_regime(regime, self.settings.scoring)
        recent = self.memory.recall(
            subject="global", kinds=[MemoryKind.REGIME], limit=1, apply_decay=False
        )
        body = (
            f"Graduation rate {regime['graduation_rate_24h']:.3%} over "
            f"{regime['sample_size']} launches, {regime['launches_per_hour']:.0f} launches/hour. "
            f"Classified {label}."
        )
        if recent and recent[0].body == body:
            return
        self.memory.remember(
            MemoryKind.REGIME,
            subject="global",
            title=f"Market regime: {label}",
            body=body,
            tags=["regime", label],
            confidence=0.8,
            supersedes=recent[0].id if recent else None,
        )

    def review_closed_positions(self) -> int:
        """Write a post-mortem for every position closed since the last review.

        This is the feedback loop that makes the memory worth keeping: the bot
        records what it believed at entry alongside what actually happened, and
        those pairs are what later heuristics are formed from.
        """
        if not self.settings.memory.autowrite_postmortems:
            return 0
        written = 0
        for position in self.broker.account.closed:
            existing = self.memory.recall(
                subject=position.token.key,
                kinds=[MemoryKind.POSTMORTEM],
                limit=1,
                apply_decay=False,
            )
            if existing:
                continue
            pnl = position.realized_pnl_native
            verdict = "profit" if pnl > 0 else "loss"
            held = (
                (position.closed_at - position.opened_at).total_seconds()
                if position.closed_at
                else 0.0
            )
            self.memory.remember(
                MemoryKind.POSTMORTEM,
                subject=position.token.key,
                title=f"{verdict} {pnl:+.4f} native on {position.token.symbol or 'token'}",
                body=(
                    f"Held {held / 60:.0f} minutes, exited via {position.exit_reason or 'unknown'}. "
                    f"Realized {pnl:+.4f} native on a {position.cost_basis_native:.4f} basis."
                ),
                tags=["postmortem", verdict, position.exit_reason or "unknown"],
                confidence=0.6,
            )
            written += 1
        return written

    # -- the sweep ---------------------------------------------------------- #

    async def _sweep_discover(self, report: SweepReport, limit: int) -> CollectionResult | None:
        """Discover into `report`, or return None if the surface raised outright."""
        try:
            discovered = await self.discover(limit)
        except Exception as exc:
            report.errors.append(f"discover failed: {exc}")
            return None
        report.discovered = len(discovered.launches)
        if discovered.degraded:
            report.degraded_surfaces.append("discover")
        if discovered.error:
            report.errors.append(discovered.error)
        return discovered

    async def _sweep_enrich(self, report: SweepReport, candidates: Sequence[Launch]) -> None:
        """Enrich the candidates. A failure costs the detail, not the sweep."""
        try:
            enriched = await self.enrich(candidates)
        except Exception as exc:
            report.errors.append(f"enrich failed: {exc}")
            return
        report.enriched = len(candidates)
        if enriched.degraded:
            report.degraded_surfaces.append("enrich")

    def _rank(self, report: SweepReport, candidates: Sequence[Launch]) -> list[tuple[float, Launch, Score]]:
        """Score every candidate, best first. One bad token does not stop the rest."""
        ranked: list[tuple[float, Launch, Score]] = []
        for launch in candidates:
            try:
                result = self.score(launch)
            except Exception as exc:
                report.errors.append(f"score failed for {launch.token.key}: {exc}")
                continue
            report.scored += 1
            ranked.append((result.composite, launch, result))
        ranked.sort(key=lambda triple: triple[0], reverse=True)
        return ranked

    def _manage_open_positions(self, report: SweepReport) -> None:
        """Mark and exit anything already open against the freshest snapshot."""
        for key in list(self.broker.account.positions.keys()):
            snapshots = self.db.snapshots_as_of(key, utcnow())
            if not snapshots:
                continue
            latest = snapshots[-1]
            position = self.broker.account.positions[key]
            self.broker.mark(position.token, latest.price_native or 0.0)
            fills = self.broker.apply_exits(position.token, latest, utcnow())
            report.exited += sum(1 for f in fills if not f.rejected)

    async def sweep(self, discover_limit: int = 60, max_candidates: int = 25) -> SweepReport:
        report = SweepReport(started_at=utcnow())

        discovered = await self._sweep_discover(report, discover_limit)
        if discovered is None:
            report.finished_at = utcnow()
            return report

        regime = self.compute_regime()
        report.regime = CompositeScorer.classify_regime(regime, self.settings.scoring)
        self.write_regime_memory(regime)

        candidates = self.screen(discovered.launches, max_candidates)
        report.screened_in = len(candidates)
        if not candidates:
            report.finished_at = utcnow()
            return report

        await self._sweep_enrich(report, candidates)

        ranked = self._rank(report, candidates)
        report.top_candidates = [
            {
                "symbol": launch.token.symbol,
                "mint": launch.token.mint,
                "score": round(result.composite, 4),
                "coverage": round(result.coverage, 3),
                "vetoes": [v.value for v in result.vetoes],
                "why": (result.explanation or "")[:160],
            }
            for _, launch, result in ranked[:10]
        ]

        for _, launch, result in ranked:
            outcome = self.decide(launch, result)
            if outcome.startswith("entered"):
                report.entered += 1

        self._manage_open_positions(report)
        self.review_closed_positions()
        report.finished_at = utcnow()
        log.info("pipeline.sweep", **report.summary())
        return report

    # -- the collection loop ------------------------------------------------ #

    def record_heartbeat(self, report: SweepReport, run_id: str | None = None) -> None:
        """Write one `collector_runs` row describing a whole sweep.

        The per-surface rows written by `enrich` say which surface failed. They
        cannot say that no sweep ran at all — a dead daemon writes nothing, and
        nothing is indistinguishable from a market where every surface happened
        to return quietly. This row is written for every sweep including the ones
        that failed outright, so the sequence of `started_at` values is a record
        of when the system was actually awake.
        """
        self.db.record_run(
            run_id=run_id or uuid.uuid4().hex,
            surface=HEARTBEAT_SURFACE,
            started_at=report.started_at,
            finished_at=report.finished_at or utcnow(),
            ok=not report.errors,
            records=report.discovered,
            error="; ".join(report.errors)[:500] or None,
        )

    @staticmethod
    def _last_sweep_seconds(session: CollectionSession) -> float:
        """How long the previous sweep took, as the estimate for the next one."""
        if not session.recent:
            return 0.0
        last = session.recent[-1]
        return ((last.finished_at or utcnow()) - last.started_at).total_seconds()

    def _stop_before_sweep(
        self,
        session: CollectionSession,
        deadline: datetime | None,
        max_sweeps: int | None,
        now: datetime,
    ) -> tuple[str | None, float | None]:
        """Why to stop before starting another sweep (or None), and the time left."""
        if max_sweeps is not None and session.sweeps >= max_sweeps:
            return "max_sweeps", None
        remaining = (deadline - now).total_seconds() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            return "deadline", remaining
        # Do not start a sweep the window has no room for. Cutting one off at the
        # deadline would write a failed heartbeat every single run, which would
        # train whoever reads the integrity panel to ignore it — and a truncated
        # sweep spends its rate-limit budget for a partial result.
        if remaining is not None and session.sweeps and remaining < self._last_sweep_seconds(session):
            return "deadline", remaining
        return None, remaining

    @staticmethod
    def _sweep_budget(default: float, remaining: float | None) -> float:
        if remaining is None:
            return default
        return min(default, remaining + DEADLINE_GRACE_SECONDS)

    async def _sweep_within_budget(
        self,
        started_at: datetime,
        budget: float,
        discover_limit: int,
        max_candidates: int,
    ) -> tuple[SweepReport, bool]:
        """Run one sweep under a hard time cap.

        Returns the report and whether an interrupt ended it. Neither Ctrl-C
        spelling is re-raised: everything the sweep wrote is already committed,
        and the point of catching it is to close the heartbeat, so that whoever
        reads this gap back next week can tell an operator stopping the daemon
        from a crash.
        """
        report = SweepReport(started_at=started_at)
        try:
            return (
                await asyncio.wait_for(self.sweep(discover_limit, max_candidates), timeout=budget),
                False,
            )
        except TimeoutError:
            report.errors.append(f"sweep exceeded its {budget:.0f}s budget and was cut off")
            log.warning("pipeline.sweep_timeout", budget_seconds=round(budget, 1))
        except (KeyboardInterrupt, asyncio.CancelledError):
            # `asyncio.run` delivers Ctrl-C by cancelling the running task, not
            # by raising KeyboardInterrupt inside it, so both spellings have to
            # be caught here.
            report.errors.append("interrupted before the sweep finished")
            report.finished_at = utcnow()
            return report, True
        except Exception as exc:
            report.errors.append(f"sweep failed: {type(exc).__name__}: {exc}")
            log.warning("pipeline.sweep_failed", error=str(exc))
        report.finished_at = utcnow()
        return report, False

    @staticmethod
    def _nap_seconds(report: SweepReport, interval_seconds: float, deadline: datetime | None) -> float:
        """Time to idle before the next sweep, never past the deadline."""
        elapsed = (utcnow() - report.started_at).total_seconds()
        nap = max(0.0, interval_seconds - elapsed)
        if deadline is not None:
            nap = min(nap, max(0.0, (deadline - utcnow()).total_seconds()))
        return nap

    async def collect(
        self,
        hours: float | None = None,
        interval_seconds: float = 60.0,
        discover_limit: int = 60,
        max_candidates: int = 25,
        max_sweeps: int | None = None,
        sweep_timeout: float | None = None,
        on_report: Any = None,
    ) -> CollectionSession:
        """Sweep on a cadence until the deadline, surviving anything a sweep does.

        Three failure modes are contained here rather than allowed to end the
        run: a sweep that raises, a sweep that hangs, and an operator's Ctrl-C.
        The first two cost one sweep and are written into the heartbeat so the
        gap is attributable afterwards; the third stops the loop cleanly with
        everything collected so far already committed.

        `hours=None` runs until `max_sweeps` is reached, or forever.
        """
        session = CollectionSession(started_at=utcnow())
        # Reachable by the caller even if this never returns normally, so an
        # interrupted run can still report what it collected.
        self.last_session = session
        deadline = session.started_at + timedelta(hours=hours) if hours is not None else None
        budget_default = (
            sweep_timeout if sweep_timeout is not None else max(180.0, interval_seconds * 3)
        )

        while True:
            now = utcnow()
            stop, remaining = self._stop_before_sweep(session, deadline, max_sweeps, now)
            if stop is not None:
                session.stopped_because = stop
                break

            budget = self._sweep_budget(budget_default, remaining)
            report, interrupted = await self._sweep_within_budget(
                now, budget, discover_limit, max_candidates
            )

            self.record_heartbeat(report)
            session.record(report)
            if interrupted:
                session.stopped_because = "interrupted"
                break
            if on_report is not None:
                on_report(report)

            nap = self._nap_seconds(report, interval_seconds, deadline)
            if nap > 0:
                try:
                    await asyncio.sleep(nap)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    session.stopped_because = "interrupted"
                    break

        session.finished_at = utcnow()
        log.info("pipeline.collect", **session.summary())
        return session

    async def run_forever(self, interval_seconds: float = 60.0, max_sweeps: int | None = None) -> None:
        """Sweep on a fixed cadence until stopped.

        Errors inside a sweep are contained and logged rather than terminating
        the loop; a collector outage should cost one sweep, not the session.
        """
        await self.collect(
            hours=None, interval_seconds=interval_seconds, max_sweeps=max_sweeps
        )


__all__ = ["CollectionSession", "Pipeline", "SweepReport", "HEARTBEAT_SURFACE"]
