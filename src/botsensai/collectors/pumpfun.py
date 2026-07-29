"""pump.fun collector.

The pump.fun API surface is sharded across four hosts with very different rate
limits, and most published documentation still points at endpoints that now
return 404. The layout below was verified live on 2026-07-25:

======================  ==========================================  ==========
host                    what it serves                              rate limit
======================  ==========================================  ==========
frontend-api-v3         coin metadata, sol-price, global-params,    50 / 60s
                        currently-live
swap-api                trades, candles, market-activity, ATH       1000 / 60s
advanced-api-v2         top holders with isDev/isSniper/isBundler   60 / 60s
livestream-api          livestream status and history               undocumented
======================  ==========================================  ==========

The rate-limit asymmetry drives the design: trade history is effectively free
and is polled aggressively, while holder forensics is the scarcest budget in the
whole system and is spent only on candidates that already passed a cheaper
screen. Each host gets its own `PacedClient` so one host's 429 storm cannot
throttle the others.

`advanced-api-v2/coins/top-holders/{mint}` deserves special mention: it returns
pre-computed `isDev`, `isSniper` and `isBundler` flags per holder. Botsensai
records them but does not depend on them — the topology metrics reconstruct the
same conclusions from raw trades, so the signal survives the endpoint being
withdrawn or rate-limited into uselessness.
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
from botsensai.util.http import PacedClient
from botsensai.util.logging import get_logger

log = get_logger(__name__)

FRONTEND_API = "https://frontend-api-v3.pump.fun"
SWAP_API = "https://swap-api.pump.fun"
ADVANCED_API = "https://advanced-api-v2.pump.fun"
LIVESTREAM_API = "https://livestream-api.pump.fun"

#: Verified from response headers, not guessed.
FRONTEND_RPM = 45      # header says 50/60s; leave headroom
SWAP_RPM = 900         # header says 1000/60s
ADVANCED_RPM = 50      # header says 60/60s — the scarce one
LIVESTREAM_RPM = 30    # undocumented; self-throttled conservatively

#: pump.fun graduates a coin at roughly 85 SOL of real reserves. Used only as a
#: fallback when the API's own progress field is absent.
GRADUATION_SOL = 85.0

#: swap-api rejects limit > 100 with HTTP 400 and returns ~20 rows per page
#: regardless, so depth is obtained by following the cursor.
TRADE_PAGE_LIMIT = 100
TRADE_PAGES = 6


def _ms_to_dt(value: Any) -> datetime | None:
    """pump.fun timestamps are milliseconds since epoch, sometimes as strings."""
    if value in (None, "", 0):
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw > 1e12:
        raw /= 1000.0
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _f(value: Any) -> float | None:
    """Coerce to float. Several pump.fun numeric fields arrive as strings."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PumpFunCollector(Collector):
    """Discovery and enrichment for pump.fun, the dominant Solana launchpad."""

    name = "pumpfun"
    description = "pump.fun launches, trades, holders, market activity and livestreams"
    can_discover = True
    can_enrich = True

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._swap: PacedClient | None = None
        self._advanced: PacedClient | None = None
        self._livestream: PacedClient | None = None
        self._sol_price_usd: float | None = None
        self._sol_price_at: datetime | None = None

    def default_headers(self) -> dict[str, str]:
        # A browser-shaped Origin/Referer pair is accepted everywhere and avoids
        # looking like an anonymous scraper, which is the cheapest possible
        # courtesy to a host that is not currently blocking us.
        return {"Origin": "https://pump.fun", "Referer": "https://pump.fun/"}

    # -- per-host clients --------------------------------------------------- #

    def _named_client(self, attr: str, base_url: str, rpm: int, cache_ttl: float) -> PacedClient:
        existing = getattr(self, attr)
        if existing is None:
            existing = PacedClient(
                f"{self.name}:{attr.lstrip('_')}",
                base_url=base_url,
                requests_per_minute=rpm,
                max_concurrency=self.config.max_concurrency,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                cache_ttl=cache_ttl,
                headers=self.default_headers(),
            )
            setattr(self, attr, existing)
        return existing

    @property
    def swap(self) -> PacedClient:
        return self._named_client("_swap", SWAP_API, SWAP_RPM, 3.0)

    @property
    def advanced(self) -> PacedClient:
        # Longer cache: this is the scarcest budget in the system.
        return self._named_client("_advanced", ADVANCED_API, ADVANCED_RPM, 20.0)

    @property
    def livestream(self) -> PacedClient:
        return self._named_client("_livestream", LIVESTREAM_API, LIVESTREAM_RPM, 15.0)

    async def aclose(self) -> None:
        await super().aclose()
        for attr in ("_swap", "_advanced", "_livestream"):
            client = getattr(self, attr)
            if client is not None:
                await client.aclose()
                setattr(self, attr, None)

    # -- helpers ------------------------------------------------------------ #

    async def sol_price_usd(self) -> float:
        """SOL/USD from pump.fun's own oracle.

        Required rather than optional: coin market caps from this API are
        denominated in SOL, and mixing them with USD liquidity figures from
        Dexscreener without converting produces silently wrong metrics.
        """
        now = utcnow()
        if (
            self._sol_price_usd is not None
            and self._sol_price_at is not None
            and (now - self._sol_price_at).total_seconds() < 60
        ):
            return self._sol_price_usd
        try:
            payload = await self.client.get_json(f"{FRONTEND_API}/sol-price", cache_ttl=45.0)
            price = _f((payload or {}).get("solPrice"))
            if price and price > 0:
                self._sol_price_usd = price
                self._sol_price_at = now
                return price
        except Exception as exc:
            log.debug("pumpfun.sol_price_failed", error=str(exc))
        return self._sol_price_usd or 150.0

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            payload = await self.client.get_json(f"{FRONTEND_API}/sol-price", cache_ttl=0.0)
            return bool(payload and payload.get("solPrice"))
        return False

    # -- parsing ------------------------------------------------------------ #

    def _coin_to_launch(self, coin: dict[str, Any]) -> Launch | None:
        mint = coin.get("mint")
        if not mint:
            return None
        created = _ms_to_dt(coin.get("created_timestamp"))
        if created is None:
            return None
        token = TokenRef(
            chain=Chain.SOLANA,
            mint=mint,
            symbol=coin.get("symbol"),
            name=coin.get("name"),
        )
        return Launch(
            token=token,
            launchpad=Launchpad.PUMPFUN,
            deployer=coin.get("creator"),
            created_at=created,
            observed_at=utcnow(),
            description=coin.get("description"),
            image_uri=coin.get("image_uri"),
            metadata_uri=coin.get("metadata_uri"),
            website=coin.get("website"),
            twitter=coin.get("twitter"),
            telegram=coin.get("telegram"),
            initial_supply=_f(coin.get("total_supply")),
            source=self.name,
        )

    def _coin_to_snapshot(self, coin: dict[str, Any], sol_usd: float) -> MarketSnapshot | None:
        mint = coin.get("mint")
        if not mint:
            return None

        complete = bool(coin.get("complete"))
        real_sol = (_f(coin.get("real_sol_reserves")) or 0.0) / 1e9
        progress = None
        if not complete:
            progress = max(0.0, min(1.0, real_sol / GRADUATION_SOL)) if real_sol else None

        virtual_sol = (_f(coin.get("virtual_sol_reserves")) or 0.0) / 1e9
        virtual_tokens = (_f(coin.get("virtual_token_reserves")) or 0.0) / 1e6
        price_native = virtual_sol / virtual_tokens if virtual_tokens > 0 else None

        market_cap_sol = _f(coin.get("market_cap"))
        usd_market_cap = _f(coin.get("usd_market_cap"))
        if usd_market_cap is None and market_cap_sol is not None:
            usd_market_cap = market_cap_sol * sol_usd

        if complete:
            stage = CurveStage.GRADUATED
        elif progress is not None and progress >= 0.85:
            stage = CurveStage.NEAR_GRADUATION
        else:
            stage = CurveStage.BONDING

        return MarketSnapshot(
            token=TokenRef(
                chain=Chain.SOLANA, mint=mint, symbol=coin.get("symbol"), name=coin.get("name")
            ),
            as_of=utcnow(),
            observed_at=utcnow(),
            stage=stage,
            price_native=price_native,
            price_usd=price_native * sol_usd if price_native else None,
            market_cap_usd=usd_market_cap,
            # Pre-graduation the tradeable depth is the curve's SOL side; there
            # is no separate pool, so reporting pool liquidity here would be
            # meaningless. Doubling the SOL side approximates the standard
            # two-sided convention other venues report.
            liquidity_usd=(real_sol + virtual_sol) * sol_usd * 2.0 if virtual_sol else None,
            bonding_curve_progress=progress,
            pair_address=coin.get("raydium_pool") or coin.get("pump_swap_pool"),
            dex="pumpswap" if complete else "pumpfun",
            source=self.name,
        )

    # -- discovery ---------------------------------------------------------- #

    async def discover(self, limit: int = 50) -> CollectionResult:
        """Newest launches, plus whatever is currently livestreaming.

        Livestreams get pulled in the same sweep because a creator who is live on
        camera is a materially different proposition from an anonymous script
        deploy, and `num_participants` is a real-time attention figure that no
        market-data API carries.
        """
        result = self._empty()
        sol_usd = await self.sol_price_usd()

        payload = await self.client.get_json(
            f"{FRONTEND_API}/coins",
            params={
                "limit": min(100, max(1, limit)),
                "offset": 0,
                "sort": "created_timestamp",
                "order": "DESC",
                "includeNsfw": "false",
            },
            cache_ttl=self.config.cache_ttl_seconds,
        )
        coins = payload if isinstance(payload, list) else (payload or {}).get("coins", [])

        for coin in coins or []:
            if not isinstance(coin, dict):
                continue
            launch = self._coin_to_launch(coin)
            if launch is None:
                continue
            result.launches.append(launch)
            snapshot = self._coin_to_snapshot(coin, sol_usd)
            if snapshot is not None:
                result.snapshots.append(snapshot)

        # Currently-live coins carry ten extra fields the plain listing lacks.
        try:
            live = await self.client.get_json(
                f"{FRONTEND_API}/coins/currently-live",
                params={"limit": 50, "offset": 0, "includeNsfw": "false"},
                cache_ttl=20.0,
            )
            live_coins = live if isinstance(live, list) else (live or {}).get("coins", [])
            seen = {launch.token.mint for launch in result.launches}
            for coin in live_coins or []:
                if not isinstance(coin, dict):
                    continue
                mint = coin.get("mint")
                if mint and mint not in seen:
                    launch = self._coin_to_launch(coin)
                    if launch is not None:
                        result.launches.append(launch)
                        seen.add(mint)
                snapshot = self._coin_to_snapshot(coin, sol_usd)
                if snapshot is not None:
                    result.snapshots.append(snapshot)
                if mint:
                    result.raw.setdefault("livestreams", {})[mint] = {
                        "num_participants": coin.get("num_participants"),
                        "livestream_title": coin.get("livestream_title"),
                        "thumbnail": coin.get("thumbnail"),
                    }
        except Exception as exc:
            result.degraded = True
            log.debug("pumpfun.currently_live_failed", error=str(exc))

        return result

    # -- enrichment --------------------------------------------------------- #

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Deepen known tokens: trades, market activity, holders, livestream state.

        Budget-aware by construction. Trades and market activity come from the
        1000/min host and are fetched for every token. Holder forensics comes
        from the 60/min host and is fetched for at most a handful per sweep, so a
        wide candidate list degrades gracefully instead of exhausting the budget
        and starving the next sweep entirely.
        """
        result = self._empty()
        sol_usd = await self.sol_price_usd()
        solana = [t for t in tokens if t.chain is Chain.SOLANA]
        if not solana:
            return result

        holder_budget = max(1, min(6, ADVANCED_RPM // 10))

        for index, token in enumerate(solana):
            mint = token.mint

            # --- trades (cheap host, paginated) -----------------------------
            # The endpoint caps `limit` at 100 and returns roughly 20 rows per
            # page regardless, so depth comes from following `nextCursor`. Depth
            # matters: the sniper and bundle metrics need the *first* trades of a
            # token's life, not the most recent twenty.
            try:
                cursor: str | None = None
                for _page in range(TRADE_PAGES):
                    params: dict[str, Any] = {"limit": TRADE_PAGE_LIMIT}
                    if cursor:
                        params["cursor"] = cursor
                    payload = await self.swap.get_json(
                        f"{SWAP_API}/v2/coins/{mint}/trades",
                        params=params,
                        cache_ttl=3.0,
                    )
                    rows = (payload or {}).get("trades", []) or []
                    for row in rows:
                        trade = self._parse_trade(row, token, sol_usd)
                        if trade is not None:
                            result.trades.append(trade)
                    pagination = (payload or {}).get("pagination") or {}
                    cursor = pagination.get("nextCursor")
                    if not rows or not cursor or not pagination.get("hasMore"):
                        break
            except Exception as exc:
                result.degraded = True
                log.debug("pumpfun.trades_failed", mint=mint, error=str(exc))

            # --- market activity (cheap host, high value) -------------------
            try:
                activity = await self.swap.get_json(
                    f"{SWAP_API}/v1/coins/{mint}/market-activity", cache_ttl=8.0
                )
                if isinstance(activity, dict) and activity:
                    result.raw.setdefault("market_activity", {})[mint] = activity
                    snapshot = self._activity_to_snapshot(activity, token, sol_usd)
                    if snapshot is not None:
                        result.snapshots.append(snapshot)
            except Exception as exc:
                result.degraded = True
                log.debug("pumpfun.activity_failed", mint=mint, error=str(exc))

            # --- holder forensics (scarce host) -----------------------------
            if index < holder_budget:
                try:
                    holders_payload = await self.advanced.get_json(
                        f"{ADVANCED_API}/coins/top-holders/{mint}", cache_ttl=25.0
                    )
                    holders, security = self._parse_holders(holders_payload, token)
                    result.holders.extend(holders)
                    if security is not None:
                        result.security.append(security)
                except Exception as exc:
                    result.degraded = True
                    log.debug("pumpfun.holders_failed", mint=mint, error=str(exc))

            # --- livestream -------------------------------------------------
            try:
                stream = await self.livestream.get_json(
                    f"{LIVESTREAM_API}/livestream", params={"mintId": mint}, cache_ttl=25.0
                )
                if isinstance(stream, dict) and stream.get("id"):
                    result.raw.setdefault("livestreams", {})[mint] = stream
                    post = self._stream_to_post(stream, token)
                    if post is not None:
                        result.posts.append(post)
            except Exception as exc:
                log.debug("pumpfun.livestream_failed", mint=mint, error=str(exc))

        return result

    # -- record parsing ----------------------------------------------------- #

    @staticmethod
    def _slot_from_index_id(value: Any) -> int | None:
        """Decode the slot from swap-api's composite ``slotIndexId``.

        The field looks like ``0004351948800012900000``: a zero-padded 12-digit
        slot followed by a 10-digit intra-slot index. Slot ordering is what the
        sniper and bundle metrics key off, and it is not exposed anywhere else
        on this endpoint, so it is worth decoding rather than discarding.
        """
        text = str(value or "")
        if len(text) < 13 or not text.isdigit():
            return None
        with contextlib.suppress(ValueError):
            slot = int(text[:12])
            return slot if slot > 0 else None
        return None

    def _parse_trade(
        self, row: dict[str, Any], token: TokenRef, sol_usd: float
    ) -> Trade | None:
        """Parse one swap-api trade row.

        Field names verified against a live response: ``tx``, ``timestamp``
        (ISO-8601, not epoch millis), ``userAddress``, ``type``, ``amountSol``,
        ``baseAmount``, ``priceSol``, ``priceUsd``, ``slotIndexId``. Legacy names
        are accepted as fallbacks because this API has already been reshaped once
        and will be again.
        """
        if not isinstance(row, dict):
            return None

        signature = (
            row.get("tx")
            or row.get("signature")
            or row.get("txSignature")
            or row.get("tx_signature")
        )
        wallet = row.get("userAddress") or row.get("user") or row.get("trader")
        if not signature or not wallet:
            return None

        raw_timestamp = row.get("timestamp")
        as_of: datetime | None = None
        if isinstance(raw_timestamp, str) and "-" in raw_timestamp:
            text = raw_timestamp.replace("Z", "+00:00")
            with contextlib.suppress(ValueError):
                parsed = datetime.fromisoformat(text)
                as_of = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if as_of is None:
            as_of = _ms_to_dt(raw_timestamp) or _ms_to_dt(row.get("blockTime"))
        if as_of is None:
            return None

        side_text = str(row.get("type") or row.get("side") or "").strip().lower()
        if side_text in ("buy", "b"):
            side = Side.BUY
        elif side_text in ("sell", "s"):
            side = Side.SELL
        else:
            is_buy = row.get("isBuy", row.get("is_buy"))
            if is_buy is None:
                return None
            side = Side.BUY if is_buy else Side.SELL

        sol_amount = (
            _f(row.get("amountSol"))
            or _f(row.get("quoteAmount"))
            or _f(row.get("solAmount"))
            or 0.0
        )
        token_amount = (
            _f(row.get("baseAmount")) or _f(row.get("tokenAmount")) or 0.0
        )
        # Older shapes of this endpoint returned lamports and micro-tokens.
        if sol_amount > 1e6:
            sol_amount /= 1e9
        if token_amount > 1e15:
            token_amount /= 1e6

        price_native = _f(row.get("priceSol"))
        if price_native is None and token_amount > 0:
            price_native = sol_amount / token_amount
        price_usd = _f(row.get("priceUsd"))
        if price_usd is None and price_native is not None:
            price_usd = price_native * sol_usd

        return Trade(
            token=token,
            signature=str(signature),
            slot=self._slot_from_index_id(row.get("slotIndexId"))
            or (int(row["slot"]) if str(row.get("slot") or "").isdigit() else None),
            as_of=as_of,
            observed_at=utcnow(),
            wallet=str(wallet),
            side=side,
            amount_token=token_amount,
            amount_native=sol_amount,
            price_native=price_native,
            price_usd=price_usd,
            source=self.name,
        )

    def _activity_to_snapshot(
        self, activity: dict[str, Any], token: TokenRef, sol_usd: float
    ) -> MarketSnapshot | None:
        """Fold the interval-keyed market-activity object into one snapshot."""

        def interval(key: str) -> dict[str, Any]:
            value = activity.get(key)
            return value if isinstance(value, dict) else {}

        five = interval("5m")
        hour = interval("1h")
        day = interval("24h")
        if not (five or hour or day):
            return None

        def volume_usd(block: dict[str, Any]) -> float | None:
            usd = _f(block.get("volumeUsd")) or _f(block.get("volume_usd"))
            if usd is not None:
                return usd
            sol = _f(block.get("volume")) or _f(block.get("volumeSol"))
            return sol * sol_usd if sol is not None else None

        def side_counts(block: dict[str, Any]) -> tuple[int | None, int | None]:
            buys = block.get("numBuys") or block.get("buys")
            sells = block.get("numSells") or block.get("sells")
            try:
                return (int(buys) if buys is not None else None,
                        int(sells) if sells is not None else None)
            except (TypeError, ValueError):
                return (None, None)

        buys_5m, sells_5m = side_counts(five)
        buys_1h, sells_1h = side_counts(hour)

        return MarketSnapshot(
            token=token,
            as_of=utcnow(),
            observed_at=utcnow(),
            volume_5m_usd=volume_usd(five),
            volume_1h_usd=volume_usd(hour),
            volume_24h_usd=volume_usd(day),
            txns_5m_buys=buys_5m,
            txns_5m_sells=sells_5m,
            txns_1h_buys=buys_1h,
            txns_1h_sells=sells_1h,
            source=f"{self.name}:market-activity",
        )

    def _parse_holders(
        self, payload: Any, token: TokenRef
    ) -> tuple[list[HolderRecord], SecurityReport | None]:
        """Parse top-holders, keeping the vendor's flags as labels only.

        `isDev`, `isSniper` and `isBundler` are recorded as labels rather than
        consumed as truth. The topology metrics derive the same conclusions from
        raw trade data, so this endpoint disappearing degrades confidence rather
        than removing the signal.
        """
        rows = None
        if isinstance(payload, dict):
            rows = payload.get("topHolders") or payload.get("holders")
        elif isinstance(payload, list):
            rows = payload
        if not rows:
            return [], None

        now = utcnow()
        total = sum(_f(r.get("amount")) or 0.0 for r in rows if isinstance(r, dict))
        if total <= 0:
            return [], None

        holders: list[HolderRecord] = []
        dev_share = 0.0
        sniper_share = 0.0
        bundled_share = 0.0

        for row in rows:
            if not isinstance(row, dict):
                continue
            address = row.get("address") or row.get("owner") or row.get("wallet")
            amount = _f(row.get("amount")) or 0.0
            if not address or amount <= 0:
                continue
            share = min(1.0, amount / total)
            labels: list[str] = []
            if row.get("isDev"):
                labels.append("creator")
                dev_share += share
            if row.get("isSniper"):
                labels.append("sniper")
                sniper_share += share
            if row.get("isBundler") or row.get("isBundled"):
                labels.append("bundler")
                bundled_share += share
            if row.get("isInsider"):
                labels.append("insider")

            holders.append(
                HolderRecord(
                    token=token,
                    as_of=now,
                    observed_at=now,
                    wallet=str(address),
                    balance=amount,
                    share_of_supply=share,
                    labels=labels,
                )
            )

        holders.sort(key=lambda h: h.share_of_supply, reverse=True)
        security = SecurityReport(
            token=token,
            as_of=now,
            observed_at=now,
            top10_share=min(1.0, sum(h.share_of_supply for h in holders[:10])),
            insider_share=min(1.0, dev_share + bundled_share),
            bundled_share=min(1.0, bundled_share),
            sniper_share=min(1.0, sniper_share),
            dev_holding_share=min(1.0, dev_share),
            # pump.fun revokes both authorities at mint by protocol design, so
            # these are structurally true rather than observed per token.
            mint_authority_revoked=True,
            freeze_authority_revoked=True,
            transfer_fee_bps=0,
            source=f"{self.name}:advanced-api-v2",
        )
        return holders, security

    def _stream_to_post(self, stream: dict[str, Any], token: TokenRef) -> SocialPost | None:
        """Represent a live stream as a social post so it flows into the metrics.

        Viewer count is a genuine, hard-to-fake attention measurement: a creator
        holding forty concurrent viewers is doing something no reply farm
        replicates.
        """
        stream_id = stream.get("id")
        started = _ms_to_dt(stream.get("streamStartTimestamp"))
        if not stream_id or started is None:
            return None
        participants = stream.get("numParticipants")
        try:
            viewers = int(participants) if participants is not None else None
        except (TypeError, ValueError):
            viewers = None

        return SocialPost(
            platform=Platform.PUMPFUN_CHAT,
            post_id=f"livestream:{stream_id}",
            author=str(stream.get("creatorAddress") or "unknown"),
            as_of=started,
            observed_at=utcnow(),
            text=str(stream.get("title") or "livestream"),
            views=viewers,
            mentioned_tokens=[token.symbol] if token.symbol else [],
            source=f"{self.name}:livestream",
        )


__all__ = [
    "ADVANCED_API",
    "FRONTEND_API",
    "GRADUATION_SOL",
    "LIVESTREAM_API",
    "SWAP_API",
    "PumpFunCollector",
]
