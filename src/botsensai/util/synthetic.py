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
    out = []
    n = int(digest[:32], 16)
    while n > 0 and len(out) < 44:
        n, rem = divmod(n, 58)
        out.append(alphabet[rem])
    return "".join(out).ljust(44, "1")


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

    token = TokenRef(chain=Chain.SOLANA, mint=mint, symbol=symbol, name=name)

    organic = archetype == "organic"
    manufactured = archetype == "manufactured"
    rug = archetype == "rug"

    launch = Launch(
        token=token,
        launchpad=Launchpad.PUMPFUN,
        deployer=deployer,
        created_at=t0,
        observed_at=t0,
        description=(
            f"{name} is a community project about {rng.choice(['bad decisions', 'municipal infrastructure', 'a damp animal', 'office life'])}. "
            "No roadmap, no promises, the art speaks for itself."
            if organic
            else f"{name} 100x gem, huge marketing, based dev, LFG"
            if manufactured
            else name
        ),
        image_uri=f"https://example.invalid/{mint[:8]}.png" if not rug else None,
        website="https://example.invalid" if organic else None,
        twitter=f"https://x.com/{symbol.lower()}coin" if not rug else None,
        telegram=f"https://t.me/{symbol.lower()}" if organic or manufactured else None,
        initial_supply=1_000_000_000.0,
        dev_buy_sol=0.5 if organic else 2.5 if manufactured else 4.0,
        source="synthetic",
    )

    result = SyntheticToken(launch=launch, archetype=archetype)

    # ---- funding structure ------------------------------------------------ #
    # Organic: many independent funders. Manufactured/rug: a few distributors.
    if organic:
        funders = [_address(rng, f"fund{i}") for i in range(28)]
    elif manufactured:
        funders = [_address(rng, f"fund{i}") for i in range(4)]
    else:
        funders = [_address(rng, "fund0"), _address(rng, "fund1")]

    n_wallets = 60 if organic else 45 if manufactured else 25
    wallets = [_address(rng, f"w{i}") for i in range(n_wallets)]
    wallet_funder: dict[str, str] = {}
    for i, w in enumerate(wallets):
        if organic:
            wallet_funder[w] = funders[i % len(funders)]
        else:
            # Heavy clustering: most wallets trace to one distributor.
            wallet_funder[w] = funders[0] if rng.random() < 0.75 else rng.choice(funders)

    # Prior trading history: organic buyers have some, farms do not.
    priors: dict[str, int] = {}
    for w in wallets:
        if organic:
            priors[w] = int(rng.lognormvariate(2.2, 1.1))
        elif manufactured:
            priors[w] = int(rng.lognormvariate(0.3, 0.6)) if rng.random() < 0.3 else 0
        else:
            priors[w] = 0
    priors[deployer] = 3 if organic else 40 if rug else 12
    result.wallet_priors = priors

    # ---- trades ----------------------------------------------------------- #
    base_slot = 300_000_000 + seed * 1000
    trades: list[Trade] = []

    def add_trade(
        wallet: str,
        side: Side,
        offset_s: float,
        native: float,
        slot_offset: int,
        bundled: bool = False,
        bundle_id: str | None = None,
        fee: int = 200_000,
    ) -> None:
        ts = t0 + timedelta(seconds=offset_s)
        price = 1e-8 * (1.0 + offset_s / 600.0)
        trades.append(
            Trade(
                token=token,
                signature=_address(rng, f"sig{len(trades)}"),
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

    # Dev buy at t=0.
    add_trade(deployer, Side.BUY, 0.5, launch.dev_buy_sol or 0.5, 0)

    if organic:
        # Steady, varied, dispersed arrival across the window.
        for i, w in enumerate(wallets[:50]):
            offset = rng.expovariate(1 / 90.0) + rng.random() * 30
            offset = min(offset * (1 + i / 25.0), horizon_seconds * 0.9)
            add_trade(
                w,
                Side.BUY,
                offset,
                round(rng.lognormvariate(-1.6, 0.9), 4),
                int(offset * 2.5),
                fee=int(rng.lognormvariate(12.2, 0.6)),
            )
        # A minority take profit, which is normal and healthy.
        for w in rng.sample(wallets[:50], 8):
            add_trade(w, Side.SELL, rng.uniform(600, horizon_seconds), round(rng.uniform(0.05, 0.3), 4), int(rng.uniform(1500, 3000)))
        result.peak_multiple = rng.uniform(3.0, 12.0)

    elif manufactured:
        # A bundle at t=0, then metronomic wash volume.
        bundle = _address(rng, "bundle")
        for w in wallets[:14]:
            add_trade(w, Side.BUY, 1.0, 0.5, 1, bundled=True, bundle_id=bundle, fee=3_000_000)
        for i, w in enumerate(wallets[14:40]):
            offset = 60.0 + i * 47.0  # metronomic: the tell
            add_trade(w, Side.BUY, offset, 0.1, int(offset * 2.5), fee=250_000)
            add_trade(w, Side.SELL, offset + 20.0, 0.1, int(offset * 2.5) + 50, fee=250_000)
        result.peak_multiple = rng.uniform(1.1, 2.0)

    else:  # rug
        bundle = _address(rng, "bundle")
        for w in wallets[:18]:
            add_trade(w, Side.BUY, 0.8, 1.2, 1, bundled=True, bundle_id=bundle, fee=5_000_000)
        for i, w in enumerate(wallets[18:24]):
            add_trade(w, Side.BUY, 120.0 + i * 15.0, 0.2, int(300 + i * 40))
        # Dev and the bundle exit together.
        dump_at = min(420.0, horizon_seconds * 0.5)
        add_trade(deployer, Side.SELL, dump_at, 4.0, int(dump_at * 2.5))
        for w in wallets[:18]:
            add_trade(w, Side.SELL, dump_at + rng.uniform(0, 30), 1.1, int(dump_at * 2.5) + 20)
        result.peak_multiple = rng.uniform(0.9, 1.4)

    result.trades = sorted(trades, key=lambda t: t.as_of)

    # ---- holders ---------------------------------------------------------- #
    # Emitted as a time series rather than a single end-state snapshot. A
    # collector polls holders repeatedly, and the topology metrics are only
    # meaningful if the backtester can see the holder set as it looked at each
    # decision point rather than as it looked at the end.
    as_of = t0 + timedelta(seconds=horizon_seconds)
    wallet_ages = {
        w: (
            rng.uniform(86400 * 30, 86400 * 900)
            if priors.get(w, 0) > 0
            else rng.uniform(60, 7200)
        )
        for w in {t.wallet for t in result.trades}
    }

    holders: list[HolderRecord] = []
    holder_steps = max(4, int(horizon_seconds // 120))
    for i in range(1, holder_steps + 1):
        snapshot_time = t0 + timedelta(seconds=i * (horizon_seconds / holder_steps))
        net: dict[str, float] = {}
        for t in result.trades:
            if t.as_of > snapshot_time:
                continue
            delta = t.amount_token if t.side is Side.BUY else -t.amount_token
            net[t.wallet] = net.get(t.wallet, 0.0) + delta
        total = sum(v for v in net.values() if v > 0) or 1.0
        for wallet, balance in net.items():
            if balance <= 0:
                continue
            holders.append(
                HolderRecord(
                    token=token,
                    as_of=snapshot_time,
                    observed_at=snapshot_time,
                    wallet=wallet,
                    balance=balance,
                    share_of_supply=min(1.0, balance / total),
                    wallet_age_seconds=wallet_ages.get(wallet),
                    funded_by=wallet_funder.get(wallet),
                    labels=["creator"] if wallet == deployer else [],
                )
            )
    result.holders = sorted(holders, key=lambda h: (h.as_of, -h.share_of_supply))

    # ---- market snapshots ------------------------------------------------- #
    snapshots: list[MarketSnapshot] = []
    steps = max(4, int(horizon_seconds // 60))
    for i in range(steps + 1):
        offset = i * (horizon_seconds / steps)
        ts = t0 + timedelta(seconds=offset)
        progress = offset / max(1.0, horizon_seconds)
        if organic:
            mult = 1.0 + (result.peak_multiple - 1.0) * math.sin(progress * math.pi * 0.8)
            liquidity = 6_000 + 40_000 * progress
        elif manufactured:
            mult = 1.0 + 0.6 * math.sin(progress * math.pi)
            liquidity = 8_000 + 6_000 * progress
        else:
            mult = 1.3 if progress < 0.5 else 0.05
            liquidity = 12_000 if progress < 0.5 else 400
        snapshots.append(
            MarketSnapshot(
                token=token,
                as_of=ts,
                observed_at=ts,
                stage=CurveStage.BONDING if progress < 0.8 else CurveStage.NEAR_GRADUATION,
                price_native=1e-8 * mult,
                price_usd=1e-8 * mult * 150.0,
                market_cap_usd=30_000 * mult,
                liquidity_usd=liquidity,
                volume_5m_usd=rng.uniform(500, 8_000) * (2.0 if organic else 1.0),
                holder_count=max(1, int(len(result.holders) * min(1.0, progress + 0.2))),
                bonding_curve_progress=min(0.95, progress * (0.9 if organic else 0.4)),
                source="synthetic",
            )
        )
    result.snapshots = snapshots

    # ---- security --------------------------------------------------------- #
    # Derive the report from the final holder slice only; `result.holders` is a
    # time series and summing across slices would double-count every wallet.
    final_slice_at = max((h.as_of for h in result.holders), default=as_of)
    final_holders = sorted(
        (h for h in result.holders if h.as_of == final_slice_at),
        key=lambda h: h.share_of_supply,
        reverse=True,
    )
    bundled_wallets = {t.wallet for t in result.trades if t.is_bundled}
    bundled_share = min(
        1.0, sum(h.share_of_supply for h in final_holders if h.wallet in bundled_wallets)
    )
    # Contract-level facts are knowable within seconds of the mint, and the
    # bundle share is knowable as soon as the bundle lands, so the report is
    # timestamped early rather than at the end of the window. Dating it at the
    # end would hide every veto from the backtester until it was too late to act.
    security_at = t0 + timedelta(seconds=45)
    result.security = SecurityReport(
        token=token,
        as_of=security_at,
        observed_at=security_at,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_burned_share=1.0 if not rug else 0.0,
        transfer_fee_bps=0,
        is_mutable_metadata=not organic,
        top10_share=min(1.0, sum(h.share_of_supply for h in final_holders[:10])),
        insider_share=0.02 if organic else 0.18 if manufactured else 0.45,
        bundled_share=bundled_share,
        dev_sold=rug,
        dev_holding_share=next(
            (h.share_of_supply for h in final_holders if h.wallet == deployer), 0.0
        ),
        source="synthetic",
    )

    # ---- social ----------------------------------------------------------- #
    posts: list[SocialPost] = []
    handle = f"{symbol.lower()}coin"
    root_id = f"root{seed}"
    posts.append(
        SocialPost(
            platform=Platform.X,
            post_id=root_id,
            author=handle,
            as_of=t0,
            observed_at=t0,
            text=launch.description or name,
            likes=40 if organic else 900,
            replies=30 if organic else 400,
            reposts=12 if organic else 350,
            quotes=8 if organic else 4,
            bookmarks=15 if organic else 2,
            views=3_000 if organic else 40_000,
            author_followers=1_200 if organic else 25_000,
            author_created_at=t0 - timedelta(days=400 if organic else 9),
            mentioned_tokens=[symbol],
            media_urls=[f"https://example.invalid/{mint[:8]}.png"],
            media_hashes=[hashlib.sha256(mint.encode()).hexdigest()[:16]],
            source="synthetic",
        )
    )

    n_replies = 40 if organic else 60 if manufactured else 12
    for i in range(n_replies):
        if organic:
            # Recombine fragments so no two replies share a skeleton. Real
            # conversation does not repeat itself, and a fixture that does would
            # make every authenticity metric look broken.
            base_text = _ORGANIC_REPLIES[i % len(_ORGANIC_REPLIES)]
            tail = rng.choice(_ORGANIC_TAILS)
            head = rng.choice(_ORGANIC_HEADS)
            text = f"{head}{base_text} {tail}".strip()
            offset = rng.expovariate(1 / 120.0) * (1 + i / 12.0)
            author_age_days = rng.choice([12, 90, 400, 900, 1800, 2600, 60, 300])
            followers = int(rng.lognormvariate(5.0, 1.6))
        else:
            text = _FARM_REPLIES[i % len(_FARM_REPLIES)].replace("$TICK", f"${symbol}")
            offset = 30.0 + i * 41.0  # metronomic cadence
            author_age_days = rng.uniform(5, 14)  # batch-provisioned fleet
            followers = int(rng.uniform(20, 120))
        offset = min(offset, horizon_seconds * 0.95)
        ts = t0 + timedelta(seconds=offset)
        posts.append(
            SocialPost(
                platform=Platform.X,
                post_id=f"r{seed}_{i}",
                author=f"user{seed}_{i}",
                as_of=ts,
                observed_at=ts,
                text=text,
                parent_id=root_id,
                likes=int(rng.uniform(0, 12)) if organic else int(rng.uniform(0, 3)),
                replies=1 if organic and rng.random() < 0.2 else 0,
                author_followers=followers,
                author_created_at=ts - timedelta(days=author_age_days),
                mentioned_tokens=[symbol] + ([_NAMES[(i + 3) % len(_NAMES)][:4].upper()] if organic and rng.random() < 0.3 else []),
                source="synthetic",
            )
        )

    # Community production: only the organic archetype generates original media
    # from independent accounts on platforms the team did not target.
    if organic:
        for i in range(7):
            ts = t0 + timedelta(seconds=rng.uniform(400, horizon_seconds))
            platform = rng.choice([Platform.INSTAGRAM, Platform.TIKTOK, Platform.REDDIT])
            posts.append(
                SocialPost(
                    platform=platform,
                    post_id=f"organic{seed}_{i}",
                    author=f"creator{seed}_{i}",
                    as_of=ts,
                    observed_at=ts,
                    text=f"made a thing about {name}, sorry",
                    likes=int(rng.uniform(20, 400)),
                    author_followers=int(rng.lognormvariate(6.5, 1.4)),
                    author_created_at=ts - timedelta(days=rng.uniform(200, 2500)),
                    media_urls=[f"https://example.invalid/remix{i}.png"],
                    media_hashes=[hashlib.sha256(f"{mint}remix{i}".encode()).hexdigest()[:16]],
                    mentioned_tokens=[symbol],
                    source="synthetic",
                )
            )

    result.posts = sorted(posts, key=lambda p: p.as_of)
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
