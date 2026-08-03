"""Dexscreener collector.

Dexscreener is treated here primarily as a *paid-promotion* surface rather than a
market-data one. Price and volume are available from several places; what only
Dexscreener publishes is a ledger of who paid for visibility and how much:

* ``/token-boosts/latest/v1`` and ``/token-boosts/top/v1`` — boost purchases,
  with cumulative ``totalAmount`` per token.
* ``/orders/v1/{chain}/{token}`` — the promotion ledger for one token: profile
  purchases, trending-bar ads, community takeovers, with status.
* ``/ads/latest/v1`` — ad placements with impressions and duration.
* ``/community-takeovers/latest/v1`` — tokens whose original dev abandoned them
  and whose community formally reclaimed them.
* ``/metas/trending/v1`` — Dexscreener's own read on which narratives are hot,
  which feeds the `meta_alignment` metric directly.

Marketing spend is genuinely informative in both directions and no other source
carries it. A token spending heavily on boosts while its organic engagement
metrics stay flat is buying the appearance of traction; a token whose organic
metrics run ahead of its promotion spend is the rarer and more interesting case.
The `boost_to_liquidity` ratio computed here is what makes that comparison.

Rate limits are documented per endpoint family and differ by a factor of five,
so the two families get separate pacers.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from botsensai.collectors.base import CollectionResult, Collector
from botsensai.config import Settings
from botsensai.models import (
    Chain,
    CurveStage,
    Launch,
    Launchpad,
    MarketSnapshot,
    TokenRef,
    utcnow,
)
from botsensai.util.http import PacedClient
from botsensai.util.logging import get_logger

log = get_logger(__name__)

BASE = "https://api.dexscreener.com"

#: Documented: 300/min for pair and search endpoints.
PAIRS_RPM = 280
#: Documented: 60/min for the profile, boost, order, ad, meta and CTO family.
PROMO_RPM = 55

CHAIN_IDS: dict[str, Chain] = {
    "solana": Chain.SOLANA,
    "ethereum": Chain.ETHEREUM,
    "base": Chain.BASE,
    "bsc": Chain.BSC,
    "blast": Chain.BLAST,
    "arbitrum": Chain.ARBITRUM,
    "abstract": Chain.ABSTRACT,
}

LAUNCHPAD_BY_DEX: dict[str, Launchpad] = {
    "pumpfun": Launchpad.PUMPFUN,
    "pumpswap": Launchpad.PUMPSWAP,
    "moonshot": Launchpad.MOONSHOT,
    "moonit": Launchpad.MOONSHOT,
    "believe": Launchpad.BELIEVE,
    "meteora": Launchpad.METEORA_DBC,
    "launchlab": Launchpad.RAYDIUM_LAUNCHLAB,
    "四meme": Launchpad.FOUR_MEME,
}


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms_to_dt(value: Any) -> datetime | None:
    raw = _f(value)
    if raw is None or raw <= 0:
        return None
    if raw > 1e12:
        raw /= 1000.0
    with contextlib.suppress(OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(raw, tz=UTC)
    return None


def _merge_boosts(boosts: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fold the two boost ledgers into one record per token.

    The same token appears in both the latest and top feeds with different
    figures; the larger is the truthful one, since both are cumulative spend
    snapshots taken at different moments.
    """
    by_token: dict[str, dict[str, Any]] = {}
    for row in boosts:
        chain_id = str(row.get("chainId") or "").lower()
        address = row.get("tokenAddress")
        if not address or chain_id not in CHAIN_IDS:
            continue
        existing = by_token.setdefault(
            f"{chain_id}:{address}",
            {
                "chainId": chain_id,
                "tokenAddress": address,
                "amount": 0.0,
                "totalAmount": 0.0,
                "description": row.get("description"),
                "links": row.get("links"),
            },
        )
        existing["amount"] = max(existing["amount"], _f(row.get("amount")) or 0.0)
        existing["totalAmount"] = max(existing["totalAmount"], _f(row.get("totalAmount")) or 0.0)
    return by_token


class DexscreenerCollector(Collector):
    """Promotion ledger, narrative metas, and cross-venue pair data."""

    name = "dexscreener"
    description = "Dexscreener boosts, paid orders, ads, CTOs, metas and pair data"
    can_discover = True
    can_enrich = True

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._promo: PacedClient | None = None

    def default_headers(self) -> dict[str, str]:
        return {"Origin": "https://dexscreener.com", "Referer": "https://dexscreener.com/"}

    @property
    def promo(self) -> PacedClient:
        """Separate pacer for the 60/min endpoint family.

        Sharing one bucket across both families would either waste the 300/min
        allowance or blow through the 60/min one; neither is acceptable.
        """
        if self._promo is None:
            self._promo = PacedClient(
                f"{self.name}:promo",
                base_url=BASE,
                requests_per_minute=PROMO_RPM,
                max_concurrency=2,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                cache_ttl=30.0,
                headers=self.default_headers(),
            )
        return self._promo

    async def aclose(self) -> None:
        await super().aclose()
        if self._promo is not None:
            await self._promo.aclose()
            self._promo = None

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            payload = await self.promo.get_json(f"{BASE}/token-boosts/latest/v1", cache_ttl=0.0)
            return isinstance(payload, list)
        return False

    # -- parsing ------------------------------------------------------------ #

    def _pair_to_records(self, pair: dict[str, Any]) -> tuple[Launch | None, MarketSnapshot | None]:
        base_token = pair.get("baseToken") or {}
        address = base_token.get("address")
        chain_id = str(pair.get("chainId") or "").lower()
        if not address:
            return None, None
        chain = CHAIN_IDS.get(chain_id)
        if chain is None:
            return None, None

        token = TokenRef(
            chain=chain,
            mint=address,
            symbol=base_token.get("symbol"),
            name=base_token.get("name"),
        )
        created = _ms_to_dt(pair.get("pairCreatedAt"))

        launch: Launch | None = None
        if created is not None:
            info = pair.get("info") or {}
            socials = {
                (s.get("type") or s.get("platform") or "").lower(): s.get("url")
                for s in (info.get("socials") or [])
                if isinstance(s, dict)
            }
            websites = info.get("websites") or []
            dex_id = str(pair.get("dexId") or "").lower()
            launch = Launch(
                token=token,
                launchpad=LAUNCHPAD_BY_DEX.get(dex_id, Launchpad.UNKNOWN),
                created_at=created,
                observed_at=utcnow(),
                image_uri=info.get("imageUrl"),
                website=(websites[0].get("url") if websites and isinstance(websites[0], dict) else None),
                twitter=socials.get("twitter") or socials.get("x"),
                telegram=socials.get("telegram"),
                source=self.name,
            )

        liquidity = pair.get("liquidity") or {}
        volume = pair.get("volume") or {}
        txns = pair.get("txns") or {}

        def side(window: str, key: str) -> int | None:
            block = txns.get(window)
            if not isinstance(block, dict):
                return None
            value = block.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        snapshot = MarketSnapshot(
            token=token,
            as_of=utcnow(),
            observed_at=utcnow(),
            stage=CurveStage.GRADUATED,
            price_usd=_f(pair.get("priceUsd")),
            price_native=_f(pair.get("priceNative")),
            market_cap_usd=_f(pair.get("marketCap")),
            fdv_usd=_f(pair.get("fdv")),
            liquidity_usd=_f(liquidity.get("usd")),
            volume_5m_usd=_f(volume.get("m5")),
            volume_1h_usd=_f(volume.get("h1")),
            volume_24h_usd=_f(volume.get("h24")),
            txns_5m_buys=side("m5", "buys"),
            txns_5m_sells=side("m5", "sells"),
            txns_1h_buys=side("h1", "buys"),
            txns_1h_sells=side("h1", "sells"),
            pair_address=pair.get("pairAddress"),
            dex=pair.get("dexId"),
            source=self.name,
        )
        return launch, snapshot

    # -- discovery ---------------------------------------------------------- #

    async def discover(self, limit: int = 50) -> CollectionResult:
        """Boosted and profiled tokens, plus the current narrative metas.

        This is not a new-launch feed — GeckoTerminal and the pump.fun websocket
        serve that better. What it discovers is the set of tokens somebody is
        actively spending money to promote, which is a different and
        complementary population.
        """
        result = self._empty()

        by_token = _merge_boosts(await self._fetch_boosts(result))
        result.raw["boosts"] = by_token

        await self._fetch_raw_list(
            result, "/token-profiles/latest/v1", "profiles", 45.0,
            event="dexscreener.profiles_failed", degrade=True, cap=200,
        )
        await self._fetch_raw_list(
            result, "/metas/trending/v1", "metas", 120.0,
            event="dexscreener.metas_failed", degrade=False,
        )
        await self._fetch_raw_list(
            result, "/community-takeovers/latest/v1", "community_takeovers", 300.0,
            event="dexscreener.cto_failed", degrade=False,
        )

        # Resolve the boosted tokens to actual pair data, batched 30 at a time.
        solana_addresses = [
            v["tokenAddress"] for v in by_token.values() if v["chainId"] == "solana"
        ][: max(0, limit)]
        await self._collect_pairs(result, "solana", solana_addresses, cache_ttl=15.0,
                                  event="dexscreener.tokens_batch_failed")
        return result

    async def _fetch_boosts(self, result: CollectionResult) -> list[dict[str, Any]]:
        """Both boost ledgers, concatenated. Either failing degrades the surface."""
        boosts: list[dict[str, Any]] = []
        for endpoint in ("/token-boosts/latest/v1", "/token-boosts/top/v1"):
            try:
                payload = await self.promo.get_json(f"{BASE}{endpoint}", cache_ttl=25.0)
                if isinstance(payload, list):
                    boosts.extend(row for row in payload if isinstance(row, dict))
            except Exception as exc:
                result.degraded = True
                log.debug("dexscreener.boosts_failed", endpoint=endpoint, error=str(exc))
        return boosts

    async def _fetch_raw_list(
        self,
        result: CollectionResult,
        endpoint: str,
        key: str,
        cache_ttl: float,
        *,
        event: str,
        degrade: bool,
        cap: int | None = None,
    ) -> None:
        """Store one list-shaped promo endpoint under ``result.raw[key]``.

        `degrade` is per-endpoint on purpose: a missing promotion ledger means
        the surface is impaired, whereas the metas and CTO feeds are commentary
        and their absence says nothing about collection health.
        """
        try:
            payload = await self.promo.get_json(f"{BASE}{endpoint}", cache_ttl=cache_ttl)
            if isinstance(payload, list):
                rows = [row for row in payload if isinstance(row, dict)]
                result.raw[key] = rows[:cap] if cap is not None else rows
        except Exception as exc:
            if degrade:
                result.degraded = True
            log.debug(event, error=str(exc))

    async def _collect_pairs(
        self,
        result: CollectionResult,
        chain_id: str,
        addresses: Sequence[str],
        *,
        cache_ttl: float,
        event: str,
    ) -> None:
        """Resolve addresses to pair records, 30 per call as the endpoint allows."""
        for start in range(0, len(addresses), 30):
            batch = addresses[start : start + 30]
            try:
                pairs = await self.client.get_json(
                    f"{BASE}/tokens/v1/{chain_id}/{','.join(batch)}", cache_ttl=cache_ttl
                )
                for pair in pairs or []:
                    if not isinstance(pair, dict):
                        continue
                    launch, snapshot = self._pair_to_records(pair)
                    if launch is not None:
                        result.launches.append(launch)
                    if snapshot is not None:
                        result.snapshots.append(snapshot)
            except Exception as exc:
                result.degraded = True
                log.debug(event, chain=chain_id, error=str(exc))

    # -- enrichment --------------------------------------------------------- #

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Pair data for known tokens, plus the paid-promotion ledger for each."""
        result = self._empty()
        if not tokens:
            return result

        by_chain: dict[str, list[str]] = {}
        for token in tokens:
            by_chain.setdefault(token.chain.value, []).append(token.mint)

        for chain_id, addresses in by_chain.items():
            await self._collect_pairs(result, chain_id, addresses, cache_ttl=10.0,
                                      event="dexscreener.enrich_pairs_failed")

        # The order ledger costs one call per token from the scarce bucket, so
        # it is only pulled for a bounded prefix of the candidate list.
        for token in list(tokens)[:20]:
            try:
                orders = await self.promo.get_json(
                    f"{BASE}/orders/v1/{token.chain.value}/{token.mint}", cache_ttl=180.0
                )
                if isinstance(orders, list) and orders:
                    result.raw.setdefault("orders", {})[token.key] = orders
            except Exception as exc:
                log.debug("dexscreener.orders_failed", token=token.key, error=str(exc))

        return result

    # -- derived signals ---------------------------------------------------- #

    @staticmethod
    def boost_to_liquidity(
        boosts: dict[str, dict[str, Any]], snapshot: MarketSnapshot
    ) -> float | None:
        """Promotion spend relative to the liquidity it is promoting.

        A high ratio means the team is spending a large fraction of the token's
        entire economic size on visibility, which is what a launch does when it
        has nothing else working. Returned as a raw ratio for the narrative
        metrics to consume.
        """
        record = boosts.get(snapshot.token.key)
        if record is None:
            return None
        total = _f(record.get("totalAmount")) or 0.0
        liquidity = snapshot.liquidity_usd or 0.0
        if liquidity <= 0:
            return None
        return total / liquidity

    @staticmethod
    def active_themes(metas: Sequence[dict[str, Any]], top_n: int = 8) -> list[tuple[str, float]]:
        """Convert the trending metas feed into (theme, weight) pairs.

        Feeds `meta_alignment` directly, which is how Dexscreener's own read on
        the current narrative rotation reaches the scorer.
        """
        scored: list[tuple[str, float]] = []
        for meta in metas:
            if not isinstance(meta, dict):
                continue
            name = meta.get("name") or meta.get("slug")
            if not name:
                continue
            market_cap = _f(meta.get("marketCap")) or 0.0
            delta = _f(meta.get("marketCapDelta")) or _f(meta.get("marketCapChange")) or 0.0
            # Momentum matters more than size: a large but flat theme is over.
            weight = 0.4 * min(1.0, market_cap / 5e8) + 0.6 * min(1.0, max(0.0, delta) / 0.5)
            description = meta.get("description") or ""
            scored.append((f"{name} {description}".strip(), min(1.0, weight)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_n]


__all__ = ["BASE", "CHAIN_IDS", "PAIRS_RPM", "PROMO_RPM", "DexscreenerCollector"]
