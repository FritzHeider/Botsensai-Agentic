"""Multi-Launchpad and Multi-DEX adapters.

Normalizes raw pool states and swap events across Pump.fun, Moonshot, Meteora Dynamic AMMs,
and Raydium CPMM into unified Launch and MarketSnapshot models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from botsensai.models import Launch, Launchpad, MarketSnapshot, TokenRef, utcnow


@dataclass
class RawPoolState:
    dex: str  # "pumpfun" | "moonshot" | "meteora" | "raydium"
    mint: str
    symbol: str | None
    name: str | None
    pool_address: str
    base_reserve: float
    quote_reserve_sol: float
    market_cap_usd: float
    liquidity_usd: float
    price_usd: float
    created_at: datetime


def normalize_pool_launch(raw: RawPoolState) -> Launch:
    """Transform diverse DEX pool creations into standardized Launch models."""
    pad_map = {
        "pumpfun": Launchpad.PUMPFUN,
        "moonshot": getattr(Launchpad, "MOONSHOT", Launchpad.PUMPFUN),
        "meteora": Launchpad.PUMPFUN,
        "raydium": getattr(Launchpad, "RAYDIUM", Launchpad.PUMPFUN),
    }
    pad = pad_map.get(raw.dex.lower(), Launchpad.PUMPFUN)
    token = TokenRef(mint=raw.mint, name=raw.name or "Unknown", symbol=raw.symbol or "TOKEN")

    return Launch(
        token=token,
        launchpad=pad,
        created_at=raw.created_at,
        observed_at=utcnow(),
        dev_buy_sol=raw.quote_reserve_sol,
    )


def normalize_market_snapshot(raw: RawPoolState, as_of: datetime | None = None) -> MarketSnapshot:
    """Transform DEX pool states into standardized MarketSnapshot models."""
    token = TokenRef(mint=raw.mint, name=raw.name or "Unknown", symbol=raw.symbol or "TOKEN")
    t = as_of or utcnow()

    return MarketSnapshot(
        token=token,
        market_cap_usd=raw.market_cap_usd,
        liquidity_usd=raw.liquidity_usd,
        price_usd=raw.price_usd,
        pair_address=raw.pool_address,
        dex=raw.dex,
        as_of=t,
        observed_at=t,
    )


__all__ = [
    "RawPoolState",
    "normalize_market_snapshot",
    "normalize_pool_launch",
]
