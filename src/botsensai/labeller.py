"""Outcome labelling: turn a token's price history into a training target.

Everything downstream of phase 1 — weight fitting, ablation, walk-forward — needs
ground truth, and ground truth for a memecoin is not "what was the peak price".
The peak is not an exit. A token that printed 40x on two hundred dollars of
liquidity produced exactly nobody a 40x, and a label that says otherwise teaches
the scorer to hunt for spikes nobody could have sold into. That is the single
most expensive mistake available here, so this module computes two numbers and
keeps them apart:

* ``max_multiple_from_t0`` — the raw price ratio. What the chart shows.
* ``max_realizable_multiple`` — what a real position of the size this system
  actually takes would have received, selling the whole thing against the curve
  at that moment. What the chart is worth.

The gap between them is the liquidity tax, and it is usually most of the number.

Two further decisions worth knowing about before reading the code:

**Absence is not zero.** If the first price we hold is an hour after the mint we
do not know the t0 price, so ``max_multiple_from_t0`` is ``None`` rather than a
confident 1.0. Same for the survival flags: they are only claimed over a horizon
the observed path actually reaches.

**Rugs are labelled conservatively.** ``Database.deployer_history`` feeds
``veto_deployer_rug_count = 1``, so a single over-eager rug label vetoes a
deployer permanently. A price fade is not a rug; a liquidity withdrawal is.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.execution.fills import CurveState
from botsensai.models import CurveStage, Launch, MarketSnapshot, Outcome, TokenRef, utcnow
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: Written to `collector_runs` so a labelling pass is visible on the dashboard
#: next to the collection surfaces.
LABEL_SURFACE = "labeller"

#: Source tag on price points reconstructed from GeckoTerminal candles.
OHLCV_SOURCE = "geckoterminal:ohlcv"

#: Survival horizons, in seconds, matching the `Outcome.survived_*` fields.
SURVIVAL_HORIZONS: tuple[tuple[str, float], ...] = (
    ("survived_1h", 3600.0),
    ("survived_24h", 86_400.0),
    ("survived_7d", 604_800.0),
)


@dataclass(frozen=True)
class PricePoint:
    """One observation of price and exit depth, in a single denomination.

    ``price`` is the representative price at ``as_of`` and ``high`` is the
    highest price touched between this point and the previous one. For a
    snapshot the two are the same number; for an OHLCV candle they are the close
    and the high, which is the only way a minute of price action inside a candle
    is visible at all.
    """

    as_of: datetime
    price: float
    high: float
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    stage: CurveStage = CurveStage.BONDING
    bonding_curve_progress: float | None = None
    source: str = "unknown"


@dataclass
class LabelPolicy:
    """Every threshold the labeller uses, in one place and named.

    These are judgement calls, not measurements, so they live here rather than
    scattered through the code where they would read as facts.
    """

    #: A launch is only labelled once it is at least this old, so the path has
    #: had time to happen.
    min_age_hours: float = 24.0

    #: How late the first price point may be and still count as "t0". Measured
    #: on the real store 2026-07-31: the median lag from mint to first snapshot
    #: is 2 minutes and 73% of launches are inside 15 minutes.
    t0_lag_tolerance_seconds: float = 900.0

    #: Size of the hypothetical position, in SOL. Defaults from
    #: `RiskSettings.max_position_native` — the realizable multiple is a
    #: function of the size actually held, so it has to come from the same
    #: place the trader's size does.
    position_size_native: float = 0.25

    #: SOL/USD, used only to turn a USD liquidity figure into curve depth.
    native_price_usd: float = 150.0

    #: A token counts as surviving a horizon if it is still worth at least this
    #: fraction of its t0 price at the end of it.
    survival_fraction: float = 0.5

    #: Rug detection. Both conditions must hold: liquidity has to have fallen to
    #: near nothing *and* have been meaningfully larger before. A token that
    #: simply faded is not labelled a rug.
    rug_residual_liquidity_usd: float = 500.0
    rug_liquidity_collapse_ratio: float = 0.25

    #: Curve progress at or above which a token is treated as graduated, for
    #: sources that report progress but never set the stage.
    graduation_progress: float = 0.999

    #: Observations closer together than this are one observation. A sweep
    #: writes from every collector at once, so five rows a second apart are five
    #: sources quoting one moment, not a price path. Measured on the real store
    #: 2026-07-31: of 383 same-token clusters inside 30 seconds, 33 disagreed by
    #: more than 3x and the worst by 6.8 million x. Read as a path, that
    #: disagreement labelled one token a 149,878x.
    path_bucket_seconds: float = 30.0

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> LabelPolicy:
        policy = cls(position_size_native=settings.risk.max_position_native)
        for key, value in overrides.items():
            if value is not None:
                setattr(policy, key, value)
        return policy


@dataclass
class LabelStats:
    """What a labelling pass did, and — more usefully — what it could not do."""

    considered: int = 0
    labelled: int = 0
    skipped_no_path: int = 0
    skipped_no_price: int = 0
    without_t0: int = 0
    ohlcv_attempts: int = 0
    ohlcv_fetched: int = 0
    ohlcv_points: int = 0
    graduated: int = 0
    rugged: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "labelled": self.labelled,
            "skipped_no_path": self.skipped_no_path,
            "skipped_no_price": self.skipped_no_price,
            "without_t0": self.without_t0,
            "ohlcv_attempts": self.ohlcv_attempts,
            "ohlcv_fetched": self.ohlcv_fetched,
            "ohlcv_points": self.ohlcv_points,
            "graduated": self.graduated,
            "rugged": self.rugged,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# Path reconstruction
# --------------------------------------------------------------------------- #


def points_from_snapshots(
    snapshots: Sequence[MarketSnapshot], denomination: str
) -> list[PricePoint]:
    """Price points from stored snapshots, in one denomination.

    Snapshots without a price in the requested denomination are dropped rather
    than converted: the `pumpfun_ws` rows are SOL-only by design and there is no
    oracle in this process, so a conversion here would be an invented number.
    """
    points: list[PricePoint] = []
    for snapshot in snapshots:
        price = snapshot.price_native if denomination == "native" else snapshot.price_usd
        if price is None or price <= 0:
            continue
        points.append(
            PricePoint(
                as_of=snapshot.as_of,
                price=float(price),
                high=float(price),
                liquidity_usd=snapshot.liquidity_usd,
                market_cap_usd=snapshot.market_cap_usd,
                stage=snapshot.stage,
                bonding_curve_progress=snapshot.bonding_curve_progress,
                source=snapshot.source,
            )
        )
    return points


def points_from_candles(
    candles: Iterable[dict[str, Any]],
    liquidity_usd: float | None = None,
    stage: CurveStage = CurveStage.BONDING,
) -> list[PricePoint]:
    """Price points from GeckoTerminal OHLCV rows.

    Candles are USD-denominated (verified live 2026-07-31 — `currency=token`
    returns a figure inconsistent with spot SOL, so it is not used). They arrive
    newest-first and with occasional duplicate timestamps, both of which are
    fixed here rather than at every call site.

    A candle says nothing about depth or lifecycle, so both are supplied by the
    caller from what the snapshots already established. That is an approximation
    and it is stated rather than hidden: with no depth every candle would model
    as infinitely deep, which is the exact fantasy this module exists to refuse.

    `stage` defaults to `BONDING` rather than `GRADUATED` deliberately.
    GeckoTerminal indexes pump.fun bonding-curve pools too, so a candle is not
    evidence of graduation, and defaulting the other way would have marked
    every OHLCV-extended token graduated whether it was or not.
    """
    by_timestamp: dict[int, dict[str, Any]] = {}
    for row in candles:
        timestamp = row.get("timestamp")
        close = row.get("close")
        if timestamp is None or close is None or float(close) <= 0:
            continue
        key = int(timestamp)
        previous = by_timestamp.get(key)
        if previous is None or float(row.get("high") or 0.0) > float(previous.get("high") or 0.0):
            by_timestamp[key] = row

    points: list[PricePoint] = []
    for timestamp in sorted(by_timestamp):
        row = by_timestamp[timestamp]
        close = float(row["close"])
        high = row.get("high")
        points.append(
            PricePoint(
                as_of=datetime.fromtimestamp(timestamp, tz=UTC),
                price=close,
                high=max(close, float(high)) if high else close,
                liquidity_usd=liquidity_usd,
                stage=stage,
                source=OHLCV_SOURCE,
            )
        )
    return points


def merge_points(*groups: Sequence[PricePoint]) -> list[PricePoint]:
    """Combine point sequences, newest observation winning on a tie.

    Snapshots are preferred over candles at the same instant because a snapshot
    carries real liquidity and a real stage; a candle carries neither.
    """
    merged: dict[float, PricePoint] = {}
    for group in groups:
        for point in group:
            key = point.as_of.timestamp()
            existing = merged.get(key)
            if existing is None or (
                existing.source == OHLCV_SOURCE and point.source != OHLCV_SOURCE
            ):
                merged[key] = point
    return [merged[k] for k in sorted(merged)]


def collapse_path(points: Sequence[PricePoint], bucket_seconds: float) -> list[PricePoint]:
    """Collapse near-simultaneous observations into one point each.

    This is the guard that keeps the labels from being made of data-quality
    noise. Every collector in a sweep writes within a second or two of every
    other, so a token picked up by pump.fun, Dexscreener and GeckoTerminal in
    the same sweep has three rows at essentially the same instant. When those
    three disagree — and on the real store 33 of 383 such clusters disagree by
    more than 3x, the worst by a factor of 6.8 million — a labeller that walks
    them in order reads the disagreement as a price move. Before this existed,
    the largest label in the store was a 149,878x that happened in nine
    microseconds.

    Buckets are formed by *gap*, not by a fixed grid, so a sweep stays together
    regardless of where its timestamps fall relative to a clock boundary.

    Within a bucket every figure is the median. Where two sources disagree by
    orders of magnitude no estimator recovers the truth, and the median does not
    pretend to; what it does is stop the disagreement from being counted as a
    move, which is the damage that actually reaches the training target.
    """
    if not points:
        return []
    ordered = sorted(points, key=lambda p: p.as_of)

    buckets: list[list[PricePoint]] = [[ordered[0]]]
    for point in ordered[1:]:
        gap = (point.as_of - buckets[-1][-1].as_of).total_seconds()
        if gap <= bucket_seconds:
            buckets[-1].append(point)
        else:
            buckets.append([point])

    collapsed: list[PricePoint] = []
    for bucket in buckets:
        if len(bucket) == 1:
            collapsed.append(bucket[0])
            continue
        price = median([p.price for p in bucket])
        # The representative point supplies the fields that are not numeric.
        # Picking the member nearest the median price keeps the stage, source
        # and progress attached to a row that actually existed.
        representative = min(bucket, key=lambda p: abs(p.price - price))
        depths = [p.liquidity_usd for p in bucket if p.liquidity_usd is not None]
        caps = [p.market_cap_usd for p in bucket if p.market_cap_usd is not None]
        progress = [p.bonding_curve_progress for p in bucket if p.bonding_curve_progress is not None]
        collapsed.append(
            PricePoint(
                as_of=representative.as_of,
                price=price,
                high=max(price, median([p.high for p in bucket])),
                liquidity_usd=median(depths) if depths else None,
                market_cap_usd=median(caps) if caps else None,
                # Graduation is monotone, so the most advanced stage in the
                # bucket is the true one; a source that has not noticed yet is
                # stale, not contradictory.
                stage=max(bucket, key=lambda p: _STAGE_ORDER.get(p.stage, 0)).stage,
                bonding_curve_progress=max(progress) if progress else None,
                source=representative.source,
            )
        )
    return collapsed


#: How far along the lifecycle each stage is, for picking the truest one in a
#: bucket. RUGGED and DEAD outrank GRADUATED: they are terminal.
_STAGE_ORDER: dict[CurveStage, int] = {
    CurveStage.PRE_LAUNCH: 0,
    CurveStage.BONDING: 1,
    CurveStage.NEAR_GRADUATION: 2,
    CurveStage.GRADUATED: 3,
    CurveStage.DEAD: 4,
    CurveStage.RUGGED: 5,
}


def choose_denomination(snapshots: Sequence[MarketSnapshot]) -> str:
    """Pick one denomination for the whole path.

    A path that mixes SOL and USD prices turns a 5% move in SOL into 5% of
    apparent alpha, so the choice is made once per token: whichever
    denomination has more usable points, native breaking the tie because it is
    the currency the position is actually denominated in and because the
    earliest price we ever hold — the bonding-curve price off the websocket — is
    SOL-only.
    """
    native = sum(1 for s in snapshots if s.price_native and s.price_native > 0)
    usd = sum(1 for s in snapshots if s.price_usd and s.price_usd > 0)
    return "native" if native >= usd else "usd"


# --------------------------------------------------------------------------- #
# Labelling
# --------------------------------------------------------------------------- #


def _curve_at(point: PricePoint, price: float, policy: LabelPolicy, denomination: str) -> CurveState:
    """Reconstruct exit depth at one point by reusing the fill model's curve."""
    snapshot = MarketSnapshot(
        token=TokenRef(mint="reconstructed"),
        as_of=point.as_of,
        price_native=price if denomination == "native" else None,
        price_usd=price if denomination == "usd" else None,
        liquidity_usd=point.liquidity_usd,
        stage=point.stage,
        bonding_curve_progress=point.bonding_curve_progress,
    )
    return CurveState.from_snapshot(snapshot, native_price_usd=policy.native_price_usd)


def realizable_multiple_at(
    point: PricePoint,
    price: float,
    tokens_held: float,
    policy: LabelPolicy,
    denomination: str,
) -> float:
    """What selling the whole position at this point would actually return.

    Expressed as a multiple of the SOL put in. The position is *entered* at the
    t0 spot price on purpose: the backtester's `FillSimulator` already charges
    entry latency, impact, fees and tips, and charging them again inside the
    training label would double-count them. What this number isolates is the
    exit side — the depth that was really there when it was time to sell.
    """
    curve = _curve_at(point, price, policy, denomination)
    sol_out = curve.sol_out_for_tokens_in(tokens_held)
    size = max(1e-12, policy.position_size_native)
    return sol_out / size


def label_from_points(
    launch: Launch,
    points: Sequence[PricePoint],
    policy: LabelPolicy,
    denomination: str = "native",
    now: datetime | None = None,
) -> Outcome | None:
    """Compute an `Outcome` from a reconstructed price path.

    Returns None when the path holds no usable price at all — an empty label is
    worse than no label, because it enters the training set as a confident zero.
    """
    # A price stamped before the token existed is not this token's price. It
    # happens for real: an OHLCV candle is stamped with the *start* of its
    # bucket, so the candle containing a mint begins before it. One bucket width
    # of grace covers that; anything earlier is another pool's history and would
    # otherwise displace the t0 point and void the multiple entirely.
    floor = launch.created_at - timedelta(seconds=policy.path_bucket_seconds)
    usable = collapse_path(
        [p for p in points if p.price > 0 and p.as_of >= floor], policy.path_bucket_seconds
    )
    if not usable:
        return None
    now = now or utcnow()

    first = usable[0]
    last = usable[-1]
    t0_lag = (first.as_of - launch.created_at).total_seconds()
    t0_price = first.price if t0_lag <= policy.t0_lag_tolerance_seconds else None

    peak_point = max(usable, key=lambda p: p.high)
    peak_price = peak_point.high

    outcome = Outcome(token=launch.token, labeled_at=now)

    # -- multiples ---------------------------------------------------------- #
    if t0_price is not None and t0_price > 0:
        outcome.max_multiple_from_t0 = peak_price / t0_price
        outcome.time_to_peak_seconds = (peak_point.as_of - launch.created_at).total_seconds()

        # The size is quoted in SOL and the path may be quoted in USD, so it is
        # converted before dividing. Dividing a SOL size by a USD price gives a
        # token count wrong by the SOL price — a 150x error in the position, and
        # therefore in every exit computed from it.
        size_in_denomination = policy.position_size_native * (
            1.0 if denomination == "native" else policy.native_price_usd
        )
        tokens_held = size_in_denomination / t0_price
        best = 0.0
        for point in usable:
            # Exit at the point's own high: the peak of the path is not
            # necessarily the best exit, because the deepest moment and the
            # highest moment are rarely the same one.
            best = max(
                best,
                realizable_multiple_at(point, point.high, tokens_held, policy, denomination),
            )
        outcome.max_realizable_multiple = best
    # else: both stay None. We do not know the t0 price, and 1.0 would be a lie.

    # -- peak and final market cap ------------------------------------------ #
    # Reported only where a source actually published a cap. There is no supply
    # figure on a price point to multiply by, and a cap reconstructed from a
    # guessed supply would look authoritative while being wrong by orders of
    # magnitude.
    outcome.peak_at = peak_point.as_of
    caps = [p for p in usable if p.market_cap_usd is not None]
    outcome.peak_market_cap_usd = max((p.market_cap_usd or 0.0) for p in caps) if caps else None
    outcome.final_market_cap_usd = caps[-1].market_cap_usd if caps else None

    # -- graduation --------------------------------------------------------- #
    for point in usable:
        graduated = point.stage is CurveStage.GRADUATED or (
            point.bonding_curve_progress is not None
            and point.bonding_curve_progress >= policy.graduation_progress
        )
        if graduated:
            outcome.graduated = True
            outcome.graduated_at = point.as_of
            break

    # -- survival ----------------------------------------------------------- #
    if t0_price is not None and t0_price > 0:
        span = (last.as_of - launch.created_at).total_seconds()
        for attribute, horizon in SURVIVAL_HORIZONS:
            if span < horizon:
                # The path does not reach this horizon. Leave the flag False and
                # count the token as unobserved rather than as a death: the
                # difference between "it died" and "we stopped watching" is the
                # whole reason these labels are worth anything.
                continue
            deadline = launch.created_at + timedelta(seconds=horizon)
            # The observation nearest the deadline, from either side. Taking the
            # last one *before* it instead would let a price from fifty minutes
            # in certify a token as alive at twenty-four hours while the very
            # next observation shows it dead. A label is computed after the fact
            # and is allowed to look forward; only features are not.
            nearest = min(usable, key=lambda p: abs((p.as_of - deadline).total_seconds()))
            setattr(outcome, attribute, nearest.price >= policy.survival_fraction * t0_price)

    # -- rug ---------------------------------------------------------------- #
    outcome.rugged = _looks_rugged(usable, policy)

    return outcome


def _looks_rugged(points: Sequence[PricePoint], policy: LabelPolicy) -> bool:
    """A rug is a liquidity withdrawal, not a disappointing chart.

    Requiring both a collapse *ratio* and a small residual keeps two different
    non-rugs out: a token that never had liquidity in the first place, and a
    large token that halved.
    """
    if any(p.stage in (CurveStage.RUGGED, CurveStage.DEAD) for p in points):
        return True

    depths = [p.liquidity_usd for p in points if p.liquidity_usd is not None]
    if len(depths) < 2:
        return False
    peak_depth = max(depths)
    final_depth = depths[-1]
    if peak_depth <= policy.rug_residual_liquidity_usd:
        return False
    return (
        final_depth <= policy.rug_residual_liquidity_usd
        and final_depth <= peak_depth * policy.rug_liquidity_collapse_ratio
    )


# --------------------------------------------------------------------------- #
# The pass over the store
# --------------------------------------------------------------------------- #


class OutcomeLabeller:
    """Walks aged launches, reconstructs each path, writes an `Outcome`."""

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        policy: LabelPolicy | None = None,
        gecko: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or Database(self.settings.path(self.settings.db_path))
        self.policy = policy or LabelPolicy.from_settings(self.settings)
        self._gecko = gecko
        self._owns_gecko = gecko is None
        self.stats = LabelStats()

    # -- collaborators ------------------------------------------------------ #

    def gecko(self) -> Any:
        if self._gecko is None:
            from botsensai.collectors.geckoterminal import GeckoTerminalCollector

            self._gecko = GeckoTerminalCollector(self.settings)
        return self._gecko

    async def aclose(self) -> None:
        if self._gecko is not None and self._owns_gecko:
            with contextlib.suppress(Exception):
                await self._gecko.aclose()

    # -- selection ---------------------------------------------------------- #

    def pending(self, limit: int | None = None, refresh: bool = False) -> list[Launch]:
        """Launches old enough to label, oldest first.

        Oldest first because the oldest are the ones whose paths are complete;
        working newest-first would spend a scarce OHLCV budget on the tokens
        with the least to say.
        """
        cutoff = utcnow() - timedelta(hours=self.policy.min_age_hours)
        clause = "" if refresh else " AND o.token_key IS NULL"
        rows = self.db.conn.execute(
            "SELECT l.* FROM launches l LEFT JOIN outcomes o ON o.token_key = l.token_key "
            "WHERE l.created_at < ?" + clause + " ORDER BY l.created_at",
            (cutoff.timestamp(),),
        ).fetchall()
        from botsensai.store.db import _row_to_launch

        launches = [_row_to_launch(r) for r in rows]
        return launches[:limit] if limit is not None else launches

    # -- one token ---------------------------------------------------------- #

    def _pool_address(self, snapshots: Sequence[MarketSnapshot]) -> str | None:
        for snapshot in reversed(snapshots):
            if snapshot.pair_address:
                return snapshot.pair_address
        return None

    def _last_liquidity(self, snapshots: Sequence[MarketSnapshot]) -> float | None:
        for snapshot in reversed(snapshots):
            if snapshot.liquidity_usd is not None:
                return snapshot.liquidity_usd
        return None

    def path_covers(self, launch: Launch, points: Sequence[PricePoint], horizon: float) -> bool:
        if not points:
            return False
        span = (max(p.as_of for p in points) - launch.created_at).total_seconds()
        return span >= horizon

    async def label_one(
        self, launch: Launch, use_ohlcv: bool = True, now: datetime | None = None
    ) -> Outcome | None:
        now = now or utcnow()
        snapshots = self.db.snapshots_as_of(launch.token.key, now)
        if not snapshots:
            self.stats.skipped_no_path += 1
            return None

        denomination = choose_denomination(snapshots)
        points = points_from_snapshots(snapshots, denomination)
        if not points:
            self.stats.skipped_no_price += 1
            return None

        # Candles are USD, so they can only extend a USD path. A native path is
        # left alone rather than silently converted at an assumed SOL price.
        needs_more = not self.path_covers(launch, points, 3600.0)
        if use_ohlcv and needs_more and denomination == "usd":
            pool = self._pool_address(snapshots)
            if pool:
                self.stats.ohlcv_attempts += 1
                candles = await self._fetch_candles(pool, launch)
                if candles:
                    self.stats.ohlcv_fetched += 1
                    extra = points_from_candles(
                        candles,
                        self._last_liquidity(snapshots),
                        stage=max(
                            snapshots, key=lambda s: _STAGE_ORDER.get(s.stage, 0)
                        ).stage,
                    )
                    self.stats.ohlcv_points += len(extra)
                    points = merge_points(points, extra)

        outcome = label_from_points(launch, points, self.policy, denomination, now=now)
        if outcome is None:
            self.stats.skipped_no_price += 1
            return None
        if outcome.max_multiple_from_t0 is None:
            self.stats.without_t0 += 1
        if outcome.graduated:
            self.stats.graduated += 1
        if outcome.rugged:
            self.stats.rugged += 1
        return outcome

    async def _fetch_candles(self, pool: str, launch: Launch) -> list[dict[str, Any]]:
        """Minute candles over the first hours of life, then hourly after.

        `before_timestamp` is the point of the first call: without it the API
        returns the *latest* hundred candles, which for a token that died on day
        one is a hundred empty minutes at the wrong end of its life.
        """
        gecko = self.gecko()
        rows: list[dict[str, Any]] = []
        anchor = int(launch.created_at.timestamp())
        try:
            rows.extend(
                await gecko.ohlcv(
                    pool,
                    timeframe="minute",
                    aggregate=1,
                    limit=100,
                    before_timestamp=anchor + 100 * 60,
                )
            )
            rows.extend(
                await gecko.ohlcv(
                    pool,
                    timeframe="hour",
                    aggregate=4,
                    limit=100,
                    before_timestamp=anchor + 400 * 3600,
                )
            )
        except Exception as exc:  # a dead pool must not stop the pass
            self.stats.errors.append(f"ohlcv {pool[:8]}: {exc}")
        return rows

    # -- the pass ----------------------------------------------------------- #

    async def run(
        self,
        limit: int | None = None,
        refresh: bool = False,
        use_ohlcv: bool = True,
        max_ohlcv: int = 40,
    ) -> LabelStats:
        """Label every pending launch, writing as it goes.

        `max_ohlcv` is a budget, not a cap on work: GeckoTerminal shares 30
        calls a minute across every endpoint in the system, and two calls per
        token means 40 tokens costs nearly three minutes of the whole stack's
        allowance. Past the budget the pass keeps labelling from stored
        snapshots alone.

        The budget counts *attempts*, not successes. Counting successes is the
        same mistake the websocket reconnect loop made with `max_connections`,
        and it fails the same way: most of these pools are dead and return
        nothing, so a budget of forty would have spent hundreds of calls
        discovering that.
        """
        started = utcnow()
        # A uuid, not a timestamp: `record_run` replaces on `run_id`, and two
        # passes inside the same second would collapse into one row.
        run_id = f"label-{uuid.uuid4().hex[:12]}"
        interrupted = False
        try:
            launches = self.pending(limit=limit, refresh=refresh)
            self.stats.considered = len(launches)

            for launch in launches:
                budget_left = self.stats.ohlcv_attempts < max_ohlcv
                try:
                    outcome = await self.label_one(launch, use_ohlcv=use_ohlcv and budget_left)
                except Exception as exc:
                    self.stats.errors.append(f"{launch.token.key[:20]}: {exc}")
                    log.debug("labeller.token_failed", token=launch.token.key, error=str(exc))
                    continue
                if outcome is None:
                    continue
                self.db.upsert_outcome(outcome)
                self.stats.labelled += 1
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Same rule as the sweep loop: a pass that left no row is
            # indistinguishable afterwards from a pass that never started, so
            # the heartbeat is written on the way out either way.
            interrupted = True
            self.stats.errors.append("interrupted")
        finally:
            self.db.record_run(
                run_id=run_id,
                surface=LABEL_SURFACE,
                started_at=started,
                finished_at=utcnow(),
                ok=not self.stats.errors,
                error="; ".join(self.stats.errors[:3]) or None,
                records=self.stats.labelled,
            )
        if interrupted:
            log.info("labeller.interrupted", labelled=self.stats.labelled)
        return self.stats


__all__ = [
    "LABEL_SURFACE",
    "OHLCV_SOURCE",
    "SURVIVAL_HORIZONS",
    "LabelPolicy",
    "LabelStats",
    "OutcomeLabeller",
    "PricePoint",
    "choose_denomination",
    "collapse_path",
    "label_from_points",
    "merge_points",
    "points_from_candles",
    "points_from_snapshots",
    "realizable_multiple_at",
]
