"""Core domain models for Botsensai.

Everything in this file is point-in-time correct by construction: every record
carries both `as_of` (the wall-clock instant the underlying fact was true) and
`observed_at` (the instant Botsensai learned it). The backtester replays on
`as_of` but is only ever allowed to *use* a record once `observed_at` has also
passed, which is what prevents look-ahead bias from leaking in through data that
was backfilled after the fact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use naive datetimes anywhere in this codebase."""
    return datetime.now(UTC)


def _ensure_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v.astimezone(UTC)


class Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        populate_by_name=True,
        ser_json_timedelta="float",
    )


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Chain(str, Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BASE = "base"
    BSC = "bsc"
    BLAST = "blast"
    ARBITRUM = "arbitrum"
    ABSTRACT = "abstract"


class Launchpad(str, Enum):
    PUMPFUN = "pumpfun"
    PUMPSWAP = "pumpswap"
    MOONSHOT = "moonshot"
    APESTORE = "apestore"
    BELIEVE = "believe"
    BONKFUN = "bonkfun"
    RAYDIUM_LAUNCHLAB = "raydium_launchlab"
    METEORA_DBC = "meteora_dbc"
    FOUR_MEME = "four_meme"
    UNKNOWN = "unknown"


class CurveStage(str, Enum):
    """Where a token sits in its lifecycle. Drives which metrics are meaningful."""

    PRE_LAUNCH = "pre_launch"
    BONDING = "bonding"
    NEAR_GRADUATION = "near_graduation"
    GRADUATED = "graduated"
    DEAD = "dead"
    RUGGED = "rugged"


class Platform(str, Enum):
    X = "x"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    REDDIT = "reddit"
    TELEGRAM = "telegram"
    YOUTUBE = "youtube"
    FOURCHAN = "fourchan"
    PUMPFUN_CHAT = "pumpfun_chat"
    DISCORD = "discord"


class Confidence(str, Enum):
    """How much a value should be trusted. Metrics computed from degraded or
    partial inputs are still emitted, but with lowered confidence so the scorer
    can down-weight rather than silently treating a gap as a zero."""

    VERIFIED = "verified"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    STALE = "stale"
    MISSING = "missing"


class Direction(str, Enum):
    HIGHER_IS_BULLISH = "higher_is_bullish"
    HIGHER_IS_BEARISH = "higher_is_bearish"
    NON_MONOTONIC = "non_monotonic"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class VetoReason(str, Enum):
    MINT_AUTHORITY_LIVE = "mint_authority_live"
    FREEZE_AUTHORITY_LIVE = "freeze_authority_live"
    LP_NOT_BURNED = "lp_not_burned"
    DEPLOYER_PRIOR_RUGS = "deployer_prior_rugs"
    INSIDER_SUPPLY_EXCESSIVE = "insider_supply_excessive"
    BUNDLE_SUPPLY_EXCESSIVE = "bundle_supply_excessive"
    HOLDER_CONCENTRATION_EXTREME = "holder_concentration_extreme"
    LIQUIDITY_BELOW_FLOOR = "liquidity_below_floor"
    TRANSFER_TAX_PRESENT = "transfer_tax_present"
    HONEYPOT_SUSPECTED = "honeypot_suspected"
    DATA_TOO_SPARSE = "data_too_sparse"
    AGE_BELOW_FLOOR = "age_below_floor"
    SOCIAL_ENGAGEMENT_INAUTHENTIC = "social_engagement_inauthentic"
    DEV_ALREADY_SOLD = "dev_already_sold"
    KILL_SWITCH = "kill_switch"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class TokenRef(Base):
    """Stable identity for a token across every subsystem."""

    chain: Chain = Chain.SOLANA
    mint: str = Field(description="Mint address (Solana) or contract address (EVM)")
    symbol: str | None = None
    name: str | None = None

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.mint}"

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(self.key)


class Launch(Base):
    """A token launch event. The anchor record every other table hangs off."""

    token: TokenRef
    launchpad: Launchpad = Launchpad.UNKNOWN
    deployer: str | None = None
    created_at: datetime
    observed_at: datetime = Field(default_factory=utcnow)

    # Launch-time descriptors, useful as features and as narrative inputs.
    description: str | None = None
    image_uri: str | None = None
    metadata_uri: str | None = None
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None

    initial_supply: float | None = None
    dev_buy_sol: float | None = Field(
        default=None, description="SOL the deployer spent on their own token at t=0"
    )
    source: str = Field(default="unknown", description="Collector that produced this record")

    _v_created = field_validator("created_at", "observed_at")(_ensure_utc)

    @property
    def age_seconds_at(self) -> Any:
        def _age(t: datetime) -> float:
            return (_ensure_utc(t) - self.created_at).total_seconds()

        return _age


# --------------------------------------------------------------------------- #
# Market state
# --------------------------------------------------------------------------- #


class MarketSnapshot(Base):
    """Point-in-time market state. One row per token per poll."""

    token: TokenRef
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)

    stage: CurveStage = CurveStage.BONDING
    price_usd: float | None = None
    price_native: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    liquidity_usd: float | None = None

    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_24h_usd: float | None = None

    txns_5m_buys: int | None = None
    txns_5m_sells: int | None = None
    txns_1h_buys: int | None = None
    txns_1h_sells: int | None = None

    holder_count: int | None = None
    bonding_curve_progress: float | None = Field(
        default=None, description="0..1 fraction of the bonding curve filled"
    )

    pair_address: str | None = None
    dex: str | None = None
    source: str = "unknown"

    _v_market = field_validator("as_of", "observed_at")(_ensure_utc)


class Trade(Base):
    """A single on-chain swap. The atom the tape-based metrics are built from."""

    token: TokenRef
    signature: str
    slot: int | None = None
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)

    wallet: str
    side: Side
    amount_token: float
    amount_native: float
    price_usd: float | None = None
    price_native: float | None = None

    priority_fee_lamports: int | None = None
    jito_tip_lamports: int | None = None
    is_bundled: bool | None = None
    bundle_id: str | None = None
    source: str = "unknown"

    _v_trade = field_validator("as_of", "observed_at")(_ensure_utc)


class HolderRecord(Base):
    """Balance for one wallet at a point in time."""

    token: TokenRef
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)
    wallet: str
    balance: float
    share_of_supply: float = Field(ge=0.0, le=1.0)
    first_seen_at: datetime | None = None
    wallet_age_seconds: float | None = None
    funded_by: str | None = None
    labels: list[str] = Field(default_factory=list)

    _v_holder = field_validator("as_of", "observed_at")(_ensure_utc)


class SecurityReport(Base):
    """Contract-level safety facts. Feeds the veto gates, not the score."""

    token: TokenRef
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)

    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_burned_share: float | None = Field(default=None, ge=0.0, le=1.0)
    lp_locked_until: datetime | None = None
    transfer_fee_bps: int | None = None
    is_mutable_metadata: bool | None = None
    top10_share: float | None = Field(default=None, ge=0.0, le=1.0)
    insider_share: float | None = Field(default=None, ge=0.0, le=1.0)
    bundled_share: float | None = Field(default=None, ge=0.0, le=1.0)
    sniper_share: float | None = Field(default=None, ge=0.0, le=1.0)
    dev_sold: bool | None = None
    dev_holding_share: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str = "unknown"

    _v_sec = field_validator("as_of", "observed_at")(_ensure_utc)


# --------------------------------------------------------------------------- #
# Social
# --------------------------------------------------------------------------- #


class SocialAccount(Base):
    platform: Platform
    handle: str
    account_id: str | None = None
    created_at: datetime | None = None
    followers: int | None = None
    following: int | None = None
    post_count: int | None = None
    verified: bool | None = None
    verified_type: str | None = None
    bio: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)

    # X's own segmentation of the follower base. `fast_followers` counts
    # followers acquired in bursts, which is what a purchased package produces —
    # about as close to a direct bought-follower oracle as any public source
    # offers, and far better evidence than inferring it from engagement ratios.
    fast_followers: int | None = None
    normal_followers: int | None = None

    @property
    def fast_follower_share(self) -> float | None:
        if self.fast_followers is None or not self.followers:
            return None
        return min(1.0, self.fast_followers / self.followers)

    @property
    def key(self) -> str:
        return f"{self.platform.value}:{self.handle.lower()}"


class SocialPost(Base):
    """A post or comment on any social surface, normalized to one shape."""

    platform: Platform
    post_id: str
    author: str
    author_id: str | None = None
    as_of: datetime = Field(description="When the post was created")
    observed_at: datetime = Field(default_factory=utcnow)

    text: str = ""
    lang: str | None = None
    url: str | None = None
    parent_id: str | None = Field(default=None, description="Set when this is a reply/comment")
    is_repost: bool = False
    quoted_id: str | None = None

    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    bookmarks: int | None = None
    views: int | None = None

    media_urls: list[str] = Field(default_factory=list)
    media_hashes: list[str] = Field(
        default_factory=list, description="Perceptual hashes, for remix/derivative detection"
    )
    author_followers: int | None = None
    author_created_at: datetime | None = None
    mentioned_tokens: list[str] = Field(default_factory=list)
    source: str = "unknown"

    _v_post = field_validator("as_of", "observed_at")(_ensure_utc)

    @property
    def engagement(self) -> int:
        return sum(v or 0 for v in (self.likes, self.replies, self.reposts, self.quotes))


class SocialBundle(Base):
    """Everything social collected for one token in one sweep."""

    token: TokenRef
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)
    posts: list[SocialPost] = Field(default_factory=list)
    accounts: list[SocialAccount] = Field(default_factory=list)
    degraded_platforms: list[Platform] = Field(
        default_factory=list, description="Platforms that failed or were rate-limited this sweep"
    )

    _v_bundle = field_validator("as_of", "observed_at")(_ensure_utc)

    def by_platform(self, platform: Platform) -> list[SocialPost]:
        return [p for p in self.posts if p.platform == platform]


# --------------------------------------------------------------------------- #
# Metrics and scoring
# --------------------------------------------------------------------------- #


class MetricValue(Base):
    """One metric evaluated for one token at one instant."""

    metric_id: str
    token: TokenRef
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)

    raw: float | None = Field(default=None, description="Value in the metric's native units")
    normalized: float | None = Field(
        default=None, ge=0.0, le=1.0, description="0..1 where 1 is maximally bullish"
    )
    confidence: Confidence = Confidence.MEDIUM
    inputs_used: list[str] = Field(default_factory=list)
    notes: str | None = None

    _v_mv = field_validator("as_of", "observed_at")(_ensure_utc)

    @property
    def usable(self) -> bool:
        return self.normalized is not None and self.confidence not in (
            Confidence.MISSING,
            Confidence.STALE,
        )


class Score(Base):
    """Composite decision output for one token at one instant."""

    token: TokenRef
    as_of: datetime
    observed_at: datetime = Field(default_factory=utcnow)

    composite: float = Field(ge=0.0, le=1.0)
    contributions: dict[str, float] = Field(default_factory=dict)
    weights_version: str = "v0"
    coverage: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Fraction of metrics that produced a usable value"
    )
    regime: str = "unknown"
    vetoes: list[VetoReason] = Field(default_factory=list)
    metric_values: list[MetricValue] = Field(default_factory=list)
    explanation: str | None = None

    _v_score = field_validator("as_of", "observed_at")(_ensure_utc)

    @property
    def vetoed(self) -> bool:
        return len(self.vetoes) > 0

    @property
    def actionable(self) -> bool:
        return not self.vetoed and self.coverage >= 0.5


# --------------------------------------------------------------------------- #
# Trading
# --------------------------------------------------------------------------- #


class Order(Base):
    token: TokenRef
    as_of: datetime
    side: Side
    size_native: float = Field(description="SOL (or native gas token) notional")
    max_slippage_bps: int = 500
    priority_fee_lamports: int = 0
    jito_tip_lamports: int = 0
    reason: str = ""
    score_at_entry: float | None = None
    client_id: str | None = None

    _v_order = field_validator("as_of")(_ensure_utc)


class Fill(Base):
    order_client_id: str | None = None
    token: TokenRef
    as_of: datetime
    side: Side
    amount_token: float
    amount_native: float
    price_native: float
    slippage_bps: float = 0.0
    fee_native: float = 0.0
    tip_native: float = 0.0
    latency_ms: float = 0.0
    rejected: bool = False
    reject_reason: str | None = None

    _v_fill = field_validator("as_of")(_ensure_utc)

    @property
    def total_cost_native(self) -> float:
        return self.amount_native + self.fee_native + self.tip_native


class Position(Base):
    token: TokenRef
    opened_at: datetime
    closed_at: datetime | None = None
    amount_token: float = 0.0
    cost_basis_native: float = 0.0
    realized_pnl_native: float = 0.0
    peak_price_native: float = 0.0
    last_price_native: float = 0.0
    fills: list[Fill] = Field(default_factory=list)
    exit_reason: str | None = None

    _v_pos = field_validator("opened_at")(_ensure_utc)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None and self.amount_token > 0

    @property
    def unrealized_pnl_native(self) -> float:
        return self.amount_token * self.last_price_native - self.cost_basis_native

    @property
    def multiple(self) -> float:
        if self.cost_basis_native <= 0:
            return 0.0
        return (self.amount_token * self.last_price_native + self.realized_pnl_native) / (
            self.cost_basis_native
        )


# --------------------------------------------------------------------------- #
# Outcomes and memory
# --------------------------------------------------------------------------- #


class Outcome(Base):
    """Ground-truth label for a launch, computed after the fact. Training target."""

    token: TokenRef
    labeled_at: datetime = Field(default_factory=utcnow)
    graduated: bool = False
    graduated_at: datetime | None = None
    rugged: bool = False
    peak_market_cap_usd: float | None = None
    peak_at: datetime | None = None
    max_multiple_from_t0: float | None = None
    time_to_peak_seconds: float | None = None
    survived_1h: bool = False
    survived_24h: bool = False
    survived_7d: bool = False
    final_market_cap_usd: float | None = None
    max_realizable_multiple: float | None = Field(
        default=None,
        description="Peak multiple achievable net of the liquidity actually available to exit",
    )

    _v_out = field_validator("labeled_at")(_ensure_utc)


class MemoryKind(str, Enum):
    OBSERVATION = "observation"
    HYPOTHESIS = "hypothesis"
    POSTMORTEM = "postmortem"
    HEURISTIC = "heuristic"
    ENTITY = "entity"
    REGIME = "regime"


class MemoryEntry(Base):
    """One durable note the bot writes to itself.

    The agentic memory layer is deliberately append-only with explicit
    supersession rather than in-place mutation, so a backtest can reconstruct
    exactly what the bot believed at any past instant.
    """

    id: str
    kind: MemoryKind
    created_at: datetime = Field(default_factory=utcnow)
    valid_from: datetime = Field(default_factory=utcnow)
    valid_until: datetime | None = None
    subject: str = Field(description="Entity this is about: token key, wallet, handle, or 'global'")
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    hit_count: int = 0
    last_used_at: datetime | None = None

    _v_mem = field_validator("created_at", "valid_from")(_ensure_utc)

    def active_at(self, t: datetime) -> bool:
        t = _ensure_utc(t)
        if t < self.valid_from:
            return False
        if self.valid_until is not None and t >= _ensure_utc(self.valid_until):
            return False
        return True


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #


class ContentPiece(Base):
    token: TokenRef | None = None
    created_at: datetime = Field(default_factory=utcnow)
    kind: str = Field(description="blog | thread | short | alert | recap")
    title: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    disclosure: str = ""
    image_prompt: str | None = None
    facts_cited: list[str] = Field(default_factory=list)
    holds_position: bool = False

    _v_content = field_validator("created_at")(_ensure_utc)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def content_hash(obj: Any) -> str:
    """Stable hash of any JSON-serializable payload, used for dedupe and caching."""
    if isinstance(obj, BaseModel):
        payload = obj.model_dump(mode="json")
    else:
        payload = obj
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "Base",
    "Chain",
    "Confidence",
    "ContentPiece",
    "CurveStage",
    "Direction",
    "Fill",
    "HolderRecord",
    "Launch",
    "Launchpad",
    "MarketSnapshot",
    "MemoryEntry",
    "MemoryKind",
    "MetricValue",
    "Order",
    "Outcome",
    "Platform",
    "Position",
    "Score",
    "SecurityReport",
    "Side",
    "SocialAccount",
    "SocialBundle",
    "SocialPost",
    "TokenRef",
    "Trade",
    "VetoReason",
    "content_hash",
    "utcnow",
]
