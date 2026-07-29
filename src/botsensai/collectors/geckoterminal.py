"""GeckoTerminal collector.

Two things make this the most valuable free surface in the stack:

* ``/networks/solana/new_pools`` gives an exact ``pool_created_at`` timestamp.
  Precise pool age is the single most important field for a strategy whose whole
  decision window is the first few minutes, and most sources either omit it or
  round it to the nearest hour.
* ``/networks/solana/tokens/{address}/info`` carries token-level safety fields
  that would otherwise cost an RPC round trip and a program-account parse per
  token: ``mint_authority``, ``freeze_authority``, ``is_honeypot``,
  ``developer_holding_percentage``, ``top_10_holder_percentage`` and a composite
  ``gt_score`` with per-category detail.

Those safety fields feed the veto gates directly, which is the highest-leverage
use of a 30-calls-per-minute budget: one call can eliminate a token entirely and
save every other collector the work.

The whole API shares a single 30/min limit across every endpoint, so this
collector is deliberately frugal — it batches through ``/pools/multi/``, caches
aggressively, and spends its safety-check budget only on tokens that have
already survived a cheaper screen.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from botsensai.collectors.base import CollectionResult, Collector
from botsensai.models import (
    Chain,
    CurveStage,
    Launch,
    Launchpad,
    MarketSnapshot,
    SecurityReport,
    TokenRef,
    utcnow,
)
from botsensai.util.logging import get_logger

log = get_logger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"

#: Documented as 30 calls/min for the whole public API, shared across endpoints.
RPM = 26

ACCEPT_HEADER = "application/json;version=20230302"


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_to_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _pool_token_address(pool: dict[str, Any], side: str = "base") -> str | None:
    """Extract the base or quote token address from a JSON:API pool object."""
    relationships = pool.get("relationships") or {}
    block = relationships.get(f"{side}_token") or {}
    data = block.get("data") or {}
    identifier = data.get("id")
    if not identifier or not isinstance(identifier, str):
        return None
    # Ids look like "solana_<address>".
    _, _, address = identifier.partition("_")
    return address or None


class GeckoTerminalCollector(Collector):
    """New-pool discovery and token-level safety screening."""

    name = "geckoterminal"
    description = "GeckoTerminal new pools, trending pools, OHLCV and token safety fields"
    can_discover = True
    can_enrich = True

    def default_headers(self) -> dict[str, str]:
        # The versioned Accept header pins the response schema; without it the
        # API is free to change field names underneath us.
        return {"Accept": ACCEPT_HEADER}

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            payload = await self.client.get_json(f"{BASE}/networks", params={"page": 1}, cache_ttl=0.0)
            return bool((payload or {}).get("data"))
        return False

    # -- parsing ------------------------------------------------------------ #

    def _pool_to_records(
        self, pool: dict[str, Any]
    ) -> tuple[Launch | None, MarketSnapshot | None]:
        attributes = pool.get("attributes") or {}
        address = _pool_token_address(pool, "base")
        if not address:
            return None, None

        name = attributes.get("name") or ""
        symbol = name.split("/")[0].strip() if "/" in name else None
        token = TokenRef(chain=Chain.SOLANA, mint=address, symbol=symbol, name=name or None)

        created = _iso_to_dt(attributes.get("pool_created_at"))
        launch: Launch | None = None
        if created is not None:
            dex_id = ""
            relationships = pool.get("relationships") or {}
            dex_block = (relationships.get("dex") or {}).get("data") or {}
            if isinstance(dex_block.get("id"), str):
                dex_id = dex_block["id"].lower()
            launchpad = Launchpad.UNKNOWN
            if "pump" in dex_id:
                launchpad = Launchpad.PUMPSWAP
            elif "moon" in dex_id:
                launchpad = Launchpad.MOONSHOT
            elif "meteora" in dex_id:
                launchpad = Launchpad.METEORA_DBC
            elif "launchlab" in dex_id:
                launchpad = Launchpad.RAYDIUM_LAUNCHLAB
            launch = Launch(
                token=token,
                launchpad=launchpad,
                created_at=created,
                observed_at=utcnow(),
                source=self.name,
            )

        volume = attributes.get("volume_usd") or {}
        transactions = attributes.get("transactions") or {}

        def txn(window: str, key: str) -> int | None:
            block = transactions.get(window)
            if not isinstance(block, dict):
                return None
            value = block.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        pool_id = pool.get("id") or ""
        _, _, pair_address = str(pool_id).partition("_")

        snapshot = MarketSnapshot(
            token=token,
            as_of=utcnow(),
            observed_at=utcnow(),
            stage=CurveStage.GRADUATED,
            price_usd=_f(attributes.get("base_token_price_usd")),
            price_native=_f(attributes.get("base_token_price_native_currency")),
            market_cap_usd=_f(attributes.get("market_cap_usd")),
            fdv_usd=_f(attributes.get("fdv_usd")),
            liquidity_usd=_f(attributes.get("reserve_in_usd")),
            volume_5m_usd=_f(volume.get("m5")),
            volume_1h_usd=_f(volume.get("h1")),
            volume_24h_usd=_f(volume.get("h24")),
            txns_5m_buys=txn("m5", "buys"),
            txns_5m_sells=txn("m5", "sells"),
            txns_1h_buys=txn("h1", "buys"),
            txns_1h_sells=txn("h1", "sells"),
            pair_address=pair_address or None,
            source=self.name,
        )
        return launch, snapshot

    def _info_to_security(self, token: TokenRef, payload: dict[str, Any]) -> SecurityReport | None:
        """Map the token info block onto a `SecurityReport`.

        Field presence varies by token, so every value is optional and absence
        is preserved as `None` rather than being coerced to a default. `None`
        means "unknown" to the veto engine, which is very different from `False`.
        """
        attributes = (payload.get("data") or {}).get("attributes") or {}
        if not attributes:
            return None

        mint_authority = attributes.get("mint_authority")
        freeze_authority = attributes.get("freeze_authority")

        def revoked(value: Any) -> bool | None:
            if value is None:
                return None
            if isinstance(value, bool):
                return not value
            text = str(value).strip().lower()
            if text in ("", "none", "null", "revoked", "false", "0"):
                return True
            return False

        dev_holding = _f(attributes.get("developer_holding_percentage"))
        top10 = _f(attributes.get("top_10_holder_percentage"))

        return SecurityReport(
            token=token,
            as_of=utcnow(),
            observed_at=utcnow(),
            mint_authority_revoked=revoked(mint_authority),
            freeze_authority_revoked=revoked(freeze_authority),
            # Percentages arrive 0..100 on this API; the model expects 0..1.
            top10_share=min(1.0, top10 / 100.0) if top10 is not None else None,
            dev_holding_share=min(1.0, dev_holding / 100.0) if dev_holding is not None else None,
            insider_share=min(1.0, dev_holding / 100.0) if dev_holding is not None else None,
            source=f"{self.name}:token-info",
        )

    # -- discovery ---------------------------------------------------------- #

    async def discover(self, limit: int = 50) -> CollectionResult:
        """New pools first, then trending — the two ends of the lifecycle.

        New pools are the entry hunting ground; trending pools tell us what the
        market is currently rewarding, which conditions the regime classifier.
        """
        result = self._empty()

        try:
            payload = await self.client.get_json(
                f"{BASE}/networks/{NETWORK}/new_pools", params={"page": 1}, cache_ttl=10.0
            )
            for pool in (payload or {}).get("data", []) or []:
                if not isinstance(pool, dict):
                    continue
                launch, snapshot = self._pool_to_records(pool)
                if launch is not None:
                    result.launches.append(launch)
                if snapshot is not None:
                    result.snapshots.append(snapshot)
        except Exception as exc:
            result.degraded = True
            result.error = str(exc)
            log.debug("geckoterminal.new_pools_failed", error=str(exc))

        try:
            payload = await self.client.get_json(
                f"{BASE}/networks/{NETWORK}/trending_pools",
                params={"duration": "5m", "page": 1},
                cache_ttl=45.0,
            )
            trending: list[dict[str, Any]] = []
            for pool in (payload or {}).get("data", []) or []:
                if not isinstance(pool, dict):
                    continue
                launch, snapshot = self._pool_to_records(pool)
                if snapshot is not None:
                    result.snapshots.append(snapshot)
                    trending.append(
                        {
                            "token_key": snapshot.token.key,
                            "name": snapshot.token.name,
                            "liquidity_usd": snapshot.liquidity_usd,
                            "volume_1h_usd": snapshot.volume_1h_usd,
                        }
                    )
            result.raw["trending_pools"] = trending
        except Exception as exc:
            result.degraded = True
            log.debug("geckoterminal.trending_failed", error=str(exc))

        return result

    # -- enrichment --------------------------------------------------------- #

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Batched pool state, then safety screening for a bounded prefix.

        The safety call is one request per token against a 30/min shared budget,
        so it is spent on at most eight candidates per sweep. Those eight are the
        ones the caller has already ranked highest, and a veto discovered here
        removes a token from every subsequent sweep, so the budget compounds.
        """
        result = self._empty()
        solana = [t for t in tokens if t.chain is Chain.SOLANA]
        if not solana:
            return result

        for start in range(0, len(solana), 30):
            batch = solana[start : start + 30]
            addresses = ",".join(t.mint for t in batch)
            try:
                payload = await self.client.get_json(
                    f"{BASE}/networks/{NETWORK}/tokens/multi/{addresses}", cache_ttl=15.0
                )
                for entry in (payload or {}).get("data", []) or []:
                    if not isinstance(entry, dict):
                        continue
                    attributes = entry.get("attributes") or {}
                    address = attributes.get("address")
                    if not address:
                        continue
                    token = TokenRef(
                        chain=Chain.SOLANA,
                        mint=address,
                        symbol=attributes.get("symbol"),
                        name=attributes.get("name"),
                    )
                    result.snapshots.append(
                        MarketSnapshot(
                            token=token,
                            as_of=utcnow(),
                            observed_at=utcnow(),
                            price_usd=_f(attributes.get("price_usd")),
                            fdv_usd=_f(attributes.get("fdv_usd")),
                            market_cap_usd=_f(attributes.get("market_cap_usd")),
                            liquidity_usd=_f(attributes.get("total_reserve_in_usd")),
                            volume_24h_usd=_f((attributes.get("volume_usd") or {}).get("h24")),
                            source=f"{self.name}:tokens-multi",
                        )
                    )
            except Exception as exc:
                result.degraded = True
                log.debug("geckoterminal.tokens_multi_failed", error=str(exc))

        for token in solana[:8]:
            try:
                info = await self.client.get_json(
                    f"{BASE}/networks/{NETWORK}/tokens/{token.mint}/info", cache_ttl=300.0
                )
                if not isinstance(info, dict):
                    continue
                security = self._info_to_security(token, info)
                if security is not None:
                    result.security.append(security)
                attributes = (info.get("data") or {}).get("attributes") or {}
                gt_score = _f(attributes.get("gt_score"))
                if gt_score is not None:
                    result.raw.setdefault("gt_scores", {})[token.key] = {
                        "gt_score": gt_score,
                        "details": attributes.get("gt_score_details"),
                        "is_honeypot": attributes.get("is_honeypot"),
                    }
            except Exception as exc:
                log.debug("geckoterminal.token_info_failed", token=token.key, error=str(exc))

        return result

    # -- extras ------------------------------------------------------------- #

    async def ohlcv(
        self, pool_address: str, timeframe: str = "minute", aggregate: int = 1, limit: int = 100
    ) -> list[dict[str, Any]]:
        """OHLCV candles for one pool. Used to build outcome labels after the fact."""
        try:
            payload = await self.client.get_json(
                f"{BASE}/networks/{NETWORK}/pools/{pool_address}/ohlcv/{timeframe}",
                params={"aggregate": aggregate, "limit": limit},
                cache_ttl=60.0,
            )
        except Exception as exc:
            log.debug("geckoterminal.ohlcv_failed", pool=pool_address, error=str(exc))
            return []
        rows = ((payload or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list", [])
        out: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, list) or len(row) < 6:
                continue
            out.append(
                {
                    "timestamp": row[0],
                    "open": _f(row[1]),
                    "high": _f(row[2]),
                    "low": _f(row[3]),
                    "close": _f(row[4]),
                    "volume": _f(row[5]),
                }
            )
        return out


__all__ = ["ACCEPT_HEADER", "BASE", "NETWORK", "RPM", "GeckoTerminalCollector"]
