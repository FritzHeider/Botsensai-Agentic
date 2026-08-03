"""Synthetic token generator.

Real launch data cannot be committed to a repository and cannot be depended on in
CI, but a metric suite with no data to run against is untested code. This module
generates internally consistent fake tokens of three archetypes — organic winner,
manufactured pump, and outright rug — with the on-chain and social structure each
archetype actually exhibits.

It is used for three things: unit tests that assert each metric separates the
archetypes, the smoke backtest that proves the whole pipeline executes, and
sizing sanity checks. It is explicitly *not* a substitute for a real backtest,
and `backtest.report` labels any run over synthetic data as such so a synthetic
result can never be mistaken for evidence.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from botsensai.models import (
    Chain,
    CurveStage,
    HolderRecord,
    Launch,
    Launchpad,
    MarketSnapshot,
    Platform,
    SecurityReport,
    Side,
    SocialPost,
    TokenRef,
    Trade,
    utcnow,
)

ARCHETYPES = ("organic", "manufactured", "rug")

_ORGANIC_REPLIES = [
    "ok the art actually got me, bought a small bag",
    "who made the second version of this, it is much funnier than the original",
    "i averaged in around 22k, down a bit but holding",
    "dev has not sold which is more than i can say for the last five of these",
    "this reminds me of that thing from last summer but better executed",
    "took profit on half, letting the rest ride honestly",
    "chart looks like death but the tg is genuinely funny",
    "someone put this on a shirt already",
    "no idea what this is but my timeline will not shut up about it",
    "sold, needed the sol for something else, no shade",
    "the remix with the cat is sending me",
    "down 40 percent and somehow still here, what is wrong with me",
]

_ORGANIC_HEADS = [
    "",
    "",
    "honestly ",
    "wait ",
    "ok so ",
    "unpopular opinion but ",
    "genuine question, ",
    "counterpoint: ",
]

_ORGANIC_TAILS = [
    "",
    "",
    "anyway",
    "idk",
    "we will see",
    "not advice obviously",
    "still think the ticker is bad though",
    "the tg mods are doing their best",
    "second time this week i have said that",
    "someone screenshot this for later",
]

_FARM_REPLIES = [
    "$TICK to the moon 🚀 next 100x",
    "$TICK to the moon 🔥 next 50x",
    "$TICK to the moon 💎 next 200x",
    "$TICK sending it 🚀 dont miss",
    "$TICK sending it 🔥 dont miss",
    "$TICK early gem 🚀 buy now",
    "$TICK early gem 💎 buy now",
    "$TICK based dev 🚀 lfg",
]

_NAMES = [
    "Chonky Capybara",
    "Wet Sock Coin",
    "Regional Manager",
    "Silent Frog",
    "Municipal Bond",
    "Uncle Larry",
    "Damp Toast",
    "Extremely Normal Dog",
]


@dataclass
class SyntheticToken:
    """A complete fake token with every record the metric layer consumes."""

    launch: Launch
    trades: list[Trade] = field(default_factory=list)
    holders: list[HolderRecord] = field(default_factory=list)
    snapshots: list[MarketSnapshot] = field(default_factory=list)
    posts: list[SocialPost] = field(default_factory=list)
    security: SecurityReport | None = None
    archetype: str = "organic"
    wallet_priors: dict[str, int] = field(default_factory=dict)
    peak_multiple: float = 1.0

    @property
    def token(self) -> TokenRef:
        return self.launch.token


def _address(rng: random.Random, prefix: str = "") -> str:
    raw = f"{prefix}{rng.random()}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    out: list[str] = []
    n = int(digest[:32], 16)
    while n > 0 and len(out) < 44:
        n, rem = divmod(n, 58)
        out.append(alphabet[rem])
    return "".join(out).ljust(44, "1")


@dataclass(frozen=True)
class _Spec:
    """The facts every section builder needs about one token being generated.

    Passed around instead of a dozen positional arguments. It carries the shared
    `rng`, so the section builders draw from the same stream in the same order
    the single long function used to — the generated corpus is unchanged by the
    split, which is what makes the refactor safe to check by diffing output.
    """

    archetype: str
    rng: random.Random
    seed: int
    token: TokenRef
    mint: str
    deployer: str
    name: str
    symbol: str
    t0: datetime
    horizon_seconds: float

    @property
    def organic(self) -> bool:
        return self.archetype == "organic"

    @property
    def manufactured(self) -> bool:
        return self.archetype == "manufactured"

    @property
    def rug(self) -> bool:
        return self.archetype == "rug"


# Per-archetype constants, as tables rather than nested ternaries. Reading one
# archetype's behaviour is now a column lookup instead of tracing an `if organic
# else ... if manufactured else ...` chain through every field.
_FUNDER_COUNT = {"organic": 28, "manufactured": 4, "rug": 2}
_WALLET_COUNT = {"organic": 60, "manufactured": 45, "rug": 25}
_DEPLOYER_PRIORS = {"organic": 3, "manufactured": 12, "rug": 40}
_DEV_BUY_SOL = {"organic": 0.5, "manufactured": 2.5, "rug": 4.0}
_INSIDER_SHARE = {"organic": 0.02, "manufactured": 0.18, "rug": 0.45}
_REPLY_COUNT = {"organic": 40, "manufactured": 60, "rug": 12}

# Root-post engagement, keyed by whether the token is organic. A manufactured
# launch buys reach it did not earn: high likes and reposts, near-zero bookmarks.
_ROOT_ENGAGEMENT: dict[bool, dict[str, int]] = {
    True: {
        "likes": 40,
        "replies": 30,
        "reposts": 12,
        "quotes": 8,
        "bookmarks": 15,
        "views": 3_000,
        "author_followers": 1_200,
        "author_age_days": 400,
    },
    False: {
        "likes": 900,
        "replies": 400,
        "reposts": 350,
        "quotes": 4,
        "bookmarks": 2,
        "views": 40_000,
        "author_followers": 25_000,
        "author_age_days": 9,
    },
}


def _description(spec: _Spec) -> str:
    if spec.organic:
        topic = spec.rng.choice(
            ["bad decisions", "municipal infrastructure", "a damp animal", "office life"]
        )
        return (
            f"{spec.name} is a community project about {topic}. "
            "No roadmap, no promises, the art speaks for itself."
        )
    if spec.manufactured:
        return f"{spec.name} 100x gem, huge marketing, based dev, LFG"
    return spec.name


def _build_launch(spec: _Spec) -> Launch:
    return Launch(
        token=spec.token,
        launchpad=Launchpad.PUMPFUN,
        deployer=spec.deployer,
        created_at=spec.t0,
        observed_at=spec.t0,
        description=_description(spec),
        image_uri=f"https://example.invalid/{spec.mint[:8]}.png" if not spec.rug else None,
        website="https://example.invalid" if spec.organic else None,
        twitter=f"https://x.com/{spec.symbol.lower()}coin" if not spec.rug else None,
        telegram=(
            f"https://t.me/{spec.symbol.lower()}"
            if spec.organic or spec.manufactured
            else None
        ),
        initial_supply=1_000_000_000.0,
        dev_buy_sol=_DEV_BUY_SOL[spec.archetype],
        source="synthetic",
    )


def _build_wallet_funding(spec: _Spec) -> tuple[list[str], dict[str, str]]:
    """Wallets and the funder each traces back to.

    Organic: many independent funders. Manufactured/rug: a few distributors, and
    most wallets trace to one of them.
    """
    rng = spec.rng
    funders = [_address(rng, f"fund{i}") for i in range(_FUNDER_COUNT[spec.archetype])]
    wallets = [_address(rng, f"w{i}") for i in range(_WALLET_COUNT[spec.archetype])]
    wallet_funder: dict[str, str] = {}
    for i, w in enumerate(wallets):
        if spec.organic:
            wallet_funder[w] = funders[i % len(funders)]
        else:
            wallet_funder[w] = funders[0] if rng.random() < 0.75 else rng.choice(funders)
    return wallets, wallet_funder


def _build_priors(spec: _Spec, wallets: list[str]) -> dict[str, int]:
    """Prior trading history: organic buyers have some, farms do not."""
    rng = spec.rng
    priors: dict[str, int] = {}
    for w in wallets:
        if spec.organic:
            priors[w] = int(rng.lognormvariate(2.2, 1.1))
        elif spec.manufactured:
            priors[w] = int(rng.lognormvariate(0.3, 0.6)) if rng.random() < 0.3 else 0
        else:
            priors[w] = 0
    priors[spec.deployer] = _DEPLOYER_PRIORS[spec.archetype]
    return priors


def _trade_adder(spec: _Spec, trades: list[Trade]) -> Callable[..., None]:
    """Return `add(...)`, which appends one internally consistent trade."""
    base_slot = 300_000_000 + spec.seed * 1000

    def add(
        wallet: str,
        side: Side,
        offset_s: float,
        native: float,
        slot_offset: int,
        bundled: bool = False,
        bundle_id: str | None = None,
        fee: int = 200_000,
    ) -> None:
        ts = spec.t0 + timedelta(seconds=offset_s)
        price = 1e-8 * (1.0 + offset_s / 600.0)
        trades.append(
            Trade(
                token=spec.token,
                signature=_address(spec.rng, f"sig{len(trades)}"),
                slot=base_slot + slot_offset,
                as_of=ts,
                observed_at=ts,
                wallet=wallet,
                side=side,
                amount_token=native / price,
                amount_native=native,
                price_native=price,
                priority_fee_lamports=fee,
                jito_tip_lamports=fee // 2 if bundled else 0,
                is_bundled=bundled,
                bundle_id=bundle_id,
                source="synthetic",
            )
        )

    return add


def _organic_trades(spec: _Spec, wallets: list[str], add: Callable[..., None]) -> float:
    """Steady, varied, dispersed arrival across the window."""
    rng = spec.rng
    for i, w in enumerate(wallets[:50]):
        offset = rng.expovariate(1 / 90.0) + rng.random() * 30
        offset = min(offset * (1 + i / 25.0), spec.horizon_seconds * 0.9)
        add(
            w,
            Side.BUY,
            offset,
            round(rng.lognormvariate(-1.6, 0.9), 4),
            int(offset * 2.5),
            fee=int(rng.lognormvariate(12.2, 0.6)),
        )
    # A minority take profit, which is normal and healthy.
    for w in rng.sample(wallets[:50], 8):
        add(
            w,
            Side.SELL,
            rng.uniform(600, spec.horizon_seconds),
            round(rng.uniform(0.05, 0.3), 4),
            int(rng.uniform(1500, 3000)),
        )
    return rng.uniform(3.0, 12.0)


def _manufactured_trades(spec: _Spec, wallets: list[str], add: Callable[..., None]) -> float:
    """A bundle at t=0, then metronomic wash volume."""
    rng = spec.rng
    bundle = _address(rng, "bundle")
    for w in wallets[:14]:
        add(w, Side.BUY, 1.0, 0.5, 1, bundled=True, bundle_id=bundle, fee=3_000_000)
    for i, w in enumerate(wallets[14:40]):
        offset = 60.0 + i * 47.0  # metronomic: the tell
        add(w, Side.BUY, offset, 0.1, int(offset * 2.5), fee=250_000)
        add(w, Side.SELL, offset + 20.0, 0.1, int(offset * 2.5) + 50, fee=250_000)
    return rng.uniform(1.1, 2.0)


def _rug_trades(spec: _Spec, wallets: list[str], add: Callable[..., None]) -> float:
    """A bundle in, a handful of organic-looking buyers, then a joint exit."""
    rng = spec.rng
    bundle = _address(rng, "bundle")
    for w in wallets[:18]:
        add(w, Side.BUY, 0.8, 1.2, 1, bundled=True, bundle_id=bundle, fee=5_000_000)
    for i, w in enumerate(wallets[18:24]):
        add(w, Side.BUY, 120.0 + i * 15.0, 0.2, int(300 + i * 40))
    # Dev and the bundle exit together.
    dump_at = min(420.0, spec.horizon_seconds * 0.5)
    add(spec.deployer, Side.SELL, dump_at, 4.0, int(dump_at * 2.5))
    for w in wallets[:18]:
        add(w, Side.SELL, dump_at + rng.uniform(0, 30), 1.1, int(dump_at * 2.5) + 20)
    return rng.uniform(0.9, 1.4)


_TRADES_BY_ARCHETYPE: dict[str, Callable[[_Spec, list[str], Callable[..., None]], float]] = {
    "organic": _organic_trades,
    "manufactured": _manufactured_trades,
    "rug": _rug_trades,
}


def _build_trades(spec: _Spec, wallets: list[str]) -> tuple[list[Trade], float]:
    """Every trade in the window, plus the peak multiple the archetype reaches."""
    trades: list[Trade] = []
    add = _trade_adder(spec, trades)
    # Dev buy at t=0.
    add(spec.deployer, Side.BUY, 0.5, _DEV_BUY_SOL[spec.archetype], 0)
    peak_multiple = _TRADES_BY_ARCHETYPE[spec.archetype](spec, wallets, add)
    return sorted(trades, key=lambda t: t.as_of), peak_multiple


def _wallet_ages(
    spec: _Spec, trades: list[Trade], priors: dict[str, int]
) -> dict[str, float]:
    rng = spec.rng
    return {
        w: (
            rng.uniform(86400 * 30, 86400 * 900)
            if priors.get(w, 0) > 0
            else rng.uniform(60, 7200)
        )
        for w in {t.wallet for t in trades}
    }


def _net_balances(trades: list[Trade], snapshot_time: datetime) -> dict[str, float]:
    """Net token balance per wallet as of `snapshot_time`."""
    net: dict[str, float] = {}
    for t in trades:
        if t.as_of > snapshot_time:
            continue
        delta = t.amount_token if t.side is Side.BUY else -t.amount_token
        net[t.wallet] = net.get(t.wallet, 0.0) + delta
    return net


def _build_holders(
    spec: _Spec,
    trades: list[Trade],
    wallet_funder: dict[str, str],
    wallet_ages: dict[str, float],
) -> list[HolderRecord]:
    """Holders as a time series rather than a single end-state snapshot.

    A collector polls holders repeatedly, and the topology metrics are only
    meaningful if the backtester can see the holder set as it looked at each
    decision point rather than as it looked at the end.
    """
    holders: list[HolderRecord] = []
    steps = max(4, int(spec.horizon_seconds // 120))
    for i in range(1, steps + 1):
        snapshot_time = spec.t0 + timedelta(seconds=i * (spec.horizon_seconds / steps))
        net = _net_balances(trades, snapshot_time)
        total = sum(v for v in net.values() if v > 0) or 1.0
        for wallet, balance in net.items():
            if balance <= 0:
                continue
            holders.append(
                HolderRecord(
                    token=spec.token,
                    as_of=snapshot_time,
                    observed_at=snapshot_time,
                    wallet=wallet,
                    balance=balance,
                    share_of_supply=min(1.0, balance / total),
                    wallet_age_seconds=wallet_ages.get(wallet),
                    funded_by=wallet_funder.get(wallet),
                    labels=["creator"] if wallet == spec.deployer else [],
                )
            )
    return sorted(holders, key=lambda h: (h.as_of, -h.share_of_supply))


def _curve_point(spec: _Spec, progress: float, peak_multiple: float) -> tuple[float, float]:
    """Price multiple and liquidity at a point through the window."""
    if spec.organic:
        return (
            1.0 + (peak_multiple - 1.0) * math.sin(progress * math.pi * 0.8),
            6_000 + 40_000 * progress,
        )
    if spec.manufactured:
        return 1.0 + 0.6 * math.sin(progress * math.pi), 8_000 + 6_000 * progress
    return (1.3, 12_000.0) if progress < 0.5 else (0.05, 400.0)


def _build_snapshots(
    spec: _Spec, holder_count: int, peak_multiple: float
) -> list[MarketSnapshot]:
    rng = spec.rng
    snapshots: list[MarketSnapshot] = []
    steps = max(4, int(spec.horizon_seconds // 60))
    for i in range(steps + 1):
        offset = i * (spec.horizon_seconds / steps)
        ts = spec.t0 + timedelta(seconds=offset)
        progress = offset / max(1.0, spec.horizon_seconds)
        mult, liquidity = _curve_point(spec, progress, peak_multiple)
        snapshots.append(
            MarketSnapshot(
                token=spec.token,
                as_of=ts,
                observed_at=ts,
                stage=CurveStage.BONDING if progress < 0.8 else CurveStage.NEAR_GRADUATION,
                price_native=1e-8 * mult,
                price_usd=1e-8 * mult * 150.0,
                market_cap_usd=30_000 * mult,
                liquidity_usd=liquidity,
                volume_5m_usd=rng.uniform(500, 8_000) * (2.0 if spec.organic else 1.0),
                holder_count=max(1, int(holder_count * min(1.0, progress + 0.2))),
                bonding_curve_progress=min(0.95, progress * (0.9 if spec.organic else 0.4)),
                source="synthetic",
            )
        )
    return snapshots


def _final_holder_slice(
    holders: list[HolderRecord], default_as_of: datetime
) -> list[HolderRecord]:
    """The last holder snapshot, largest share first.

    `holders` is a time series, and summing across slices would double-count
    every wallet — the security report must be derived from one slice only.
    """
    final_slice_at = max((h.as_of for h in holders), default=default_as_of)
    return sorted(
        (h for h in holders if h.as_of == final_slice_at),
        key=lambda h: h.share_of_supply,
        reverse=True,
    )


def _build_security(
    spec: _Spec,
    holders: list[HolderRecord],
    trades: list[Trade],
    default_as_of: datetime,
) -> SecurityReport:
    final_holders = _final_holder_slice(holders, default_as_of)
    bundled_wallets = {t.wallet for t in trades if t.is_bundled}
    bundled_share = min(
        1.0, sum(h.share_of_supply for h in final_holders if h.wallet in bundled_wallets)
    )
    # Contract-level facts are knowable within seconds of the mint, and the
    # bundle share is knowable as soon as the bundle lands, so the report is
    # timestamped early rather than at the end of the window. Dating it at the
    # end would hide every veto from the backtester until it was too late to act.
    security_at = spec.t0 + timedelta(seconds=45)
    return SecurityReport(
        token=spec.token,
        as_of=security_at,
        observed_at=security_at,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_burned_share=1.0 if not spec.rug else 0.0,
        transfer_fee_bps=0,
        is_mutable_metadata=not spec.organic,
        top10_share=min(1.0, sum(h.share_of_supply for h in final_holders[:10])),
        insider_share=_INSIDER_SHARE[spec.archetype],
        bundled_share=bundled_share,
        dev_sold=spec.rug,
        dev_holding_share=next(
            (h.share_of_supply for h in final_holders if h.wallet == spec.deployer), 0.0
        ),
        source="synthetic",
    )


def _root_post(spec: _Spec, description: str, root_id: str) -> SocialPost:
    engagement = _ROOT_ENGAGEMENT[spec.organic]
    return SocialPost(
        platform=Platform.X,
        post_id=root_id,
        author=f"{spec.symbol.lower()}coin",
        as_of=spec.t0,
        observed_at=spec.t0,
        text=description,
        likes=engagement["likes"],
        replies=engagement["replies"],
        reposts=engagement["reposts"],
        quotes=engagement["quotes"],
        bookmarks=engagement["bookmarks"],
        views=engagement["views"],
        author_followers=engagement["author_followers"],
        author_created_at=spec.t0 - timedelta(days=engagement["author_age_days"]),
        mentioned_tokens=[spec.symbol],
        media_urls=[f"https://example.invalid/{spec.mint[:8]}.png"],
        media_hashes=[hashlib.sha256(spec.mint.encode()).hexdigest()[:16]],
        source="synthetic",
    )


def _reply_posts(spec: _Spec, root_id: str) -> list[SocialPost]:
    rng = spec.rng
    posts: list[SocialPost] = []
    for i in range(_REPLY_COUNT[spec.archetype]):
        if spec.organic:
            # Recombine fragments so no two replies share a skeleton. Real
            # conversation does not repeat itself, and a fixture that does would
            # make every authenticity metric look broken.
            base_text = _ORGANIC_REPLIES[i % len(_ORGANIC_REPLIES)]
            tail = rng.choice(_ORGANIC_TAILS)
            head = rng.choice(_ORGANIC_HEADS)
            text = f"{head}{base_text} {tail}".strip()
            offset = rng.expovariate(1 / 120.0) * (1 + i / 12.0)
            author_age_days: float = rng.choice([12, 90, 400, 900, 1800, 2600, 60, 300])
            followers = int(rng.lognormvariate(5.0, 1.6))
        else:
            text = _FARM_REPLIES[i % len(_FARM_REPLIES)].replace("$TICK", f"${spec.symbol}")
            offset = 30.0 + i * 41.0  # metronomic cadence
            author_age_days = rng.uniform(5, 14)  # batch-provisioned fleet
            followers = int(rng.uniform(20, 120))
        offset = min(offset, spec.horizon_seconds * 0.95)
        ts = spec.t0 + timedelta(seconds=offset)
        posts.append(
            SocialPost(
                platform=Platform.X,
                post_id=f"r{spec.seed}_{i}",
                author=f"user{spec.seed}_{i}",
                as_of=ts,
                observed_at=ts,
                text=text,
                parent_id=root_id,
                likes=int(rng.uniform(0, 12)) if spec.organic else int(rng.uniform(0, 3)),
                replies=1 if spec.organic and rng.random() < 0.2 else 0,
                author_followers=followers,
                author_created_at=ts - timedelta(days=author_age_days),
                mentioned_tokens=[spec.symbol]
                + (
                    [_NAMES[(i + 3) % len(_NAMES)][:4].upper()]
                    if spec.organic and rng.random() < 0.3
                    else []
                ),
                source="synthetic",
            )
        )
    return posts


def _community_posts(spec: _Spec) -> list[SocialPost]:
    """Only the organic archetype generates original media.

    Independent accounts, on platforms the team did not target.
    """
    if not spec.organic:
        return []
    rng = spec.rng
    posts: list[SocialPost] = []
    for i in range(7):
        ts = spec.t0 + timedelta(seconds=rng.uniform(400, spec.horizon_seconds))
        platform = rng.choice([Platform.INSTAGRAM, Platform.TIKTOK, Platform.REDDIT])
        posts.append(
            SocialPost(
                platform=platform,
                post_id=f"organic{spec.seed}_{i}",
                author=f"creator{spec.seed}_{i}",
                as_of=ts,
                observed_at=ts,
                text=f"made a thing about {spec.name}, sorry",
                likes=int(rng.uniform(20, 400)),
                author_followers=int(rng.lognormvariate(6.5, 1.4)),
                author_created_at=ts - timedelta(days=rng.uniform(200, 2500)),
                media_urls=[f"https://example.invalid/remix{i}.png"],
                media_hashes=[
                    hashlib.sha256(f"{spec.mint}remix{i}".encode()).hexdigest()[:16]
                ],
                mentioned_tokens=[spec.symbol],
                source="synthetic",
            )
        )
    return posts


def _build_posts(spec: _Spec, description: str) -> list[SocialPost]:
    root_id = f"root{spec.seed}"
    posts = [_root_post(spec, description, root_id)]
    posts.extend(_reply_posts(spec, root_id))
    posts.extend(_community_posts(spec))
    return sorted(posts, key=lambda p: p.as_of)


def generate_token(
    archetype: str = "organic",
    *,
    seed: int = 0,
    created_at: datetime | None = None,
    horizon_seconds: float = 3600.0,
) -> SyntheticToken:
    """Build one internally consistent synthetic token of the given archetype."""
    if archetype not in ARCHETYPES:
        raise ValueError(f"unknown archetype {archetype!r}; expected one of {ARCHETYPES}")

    rng = random.Random(seed * 7919 + hash(archetype) % 10_000)
    t0 = created_at or (utcnow() - timedelta(seconds=horizon_seconds))
    mint = _address(rng, "mint")
    deployer = _address(rng, "dev")
    name = _NAMES[seed % len(_NAMES)]
    symbol = "".join(w[0] for w in name.split())[:5].upper() + str(seed % 10)

    spec = _Spec(
        archetype=archetype,
        rng=rng,
        seed=seed,
        token=TokenRef(chain=Chain.SOLANA, mint=mint, symbol=symbol, name=name),
        mint=mint,
        deployer=deployer,
        name=name,
        symbol=symbol,
        t0=t0,
        horizon_seconds=horizon_seconds,
    )

    # Section order is load-bearing: every builder draws from the same `rng`, so
    # reordering these calls would change the corpus even though nothing else did.
    launch = _build_launch(spec)
    result = SyntheticToken(launch=launch, archetype=archetype)
    wallets, wallet_funder = _build_wallet_funding(spec)
    result.wallet_priors = _build_priors(spec, wallets)
    result.trades, result.peak_multiple = _build_trades(spec, wallets)
    result.holders = _build_holders(
        spec,
        result.trades,
        wallet_funder,
        _wallet_ages(spec, result.trades, result.wallet_priors),
    )
    result.snapshots = _build_snapshots(spec, len(result.holders), result.peak_multiple)
    result.security = _build_security(
        spec, result.holders, result.trades, t0 + timedelta(seconds=horizon_seconds)
    )
    result.posts = _build_posts(spec, launch.description or name)
    return result


def generate_cohort(
    n: int = 30,
    *,
    seed: int = 1337,
    organic_share: float = 0.2,
    manufactured_share: float = 0.45,
    created_at: datetime | None = None,
    spacing_seconds: float = 300.0,
) -> list[SyntheticToken]:
    """A population with a realistic archetype mix.

    The default mix reflects the empirical reality that the overwhelming majority
    of launches are manufactured or outright rugs. A test population that is half
    winners produces a scorer that looks excellent and loses money.
    """
    rng = random.Random(seed)
    base = created_at or (utcnow() - timedelta(hours=6))
    out: list[SyntheticToken] = []
    for i in range(n):
        roll = rng.random()
        if roll < organic_share:
            archetype = "organic"
        elif roll < organic_share + manufactured_share:
            archetype = "manufactured"
        else:
            archetype = "rug"
        out.append(
            generate_token(
                archetype,
                seed=seed + i,
                created_at=base + timedelta(seconds=i * spacing_seconds),
            )
        )
    return out


__all__ = ["ARCHETYPES", "SyntheticToken", "generate_cohort", "generate_token"]
