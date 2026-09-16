"""Birdeye token security and holder distribution collector.

Consumes Birdeye REST API using `settings.birdeye_api_key` to enrich tokens with:
- Contract security audit (mint authority, freeze authority, honeypot flags).
- Top-10 / whale holder concentration percentages.
- Creator / developer wallet retention share.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from botsensai.collectors.base import CollectionResult, Collector
from botsensai.config import Settings
from botsensai.models import (
    HolderRecord,
    SecurityReport,
    TokenRef,
    utcnow,
)
from botsensai.util.http import HttpError, PacedClient
from botsensai.util.logging import get_logger

log = get_logger(__name__)

BIRDEYE_BASE_URL = "https://public-api.birdeye.so"


class BirdeyeCollector(Collector):
    """Enriches tokens with verified contract security facts and holder metrics."""

    name = "birdeye"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings=settings)
        self.api_key = self.settings.birdeye_api_key

    def default_headers(self) -> dict[str, str]:
        headers = {}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
            headers["x-chain"] = "solana"
        return headers

    @property
    def client(self) -> PacedClient:
        if self._client is None:
            cfg = self.config
            self._client = PacedClient(
                self.name,
                base_url=cfg.base_url or BIRDEYE_BASE_URL,
                requests_per_minute=cfg.requests_per_minute,
                max_concurrency=cfg.max_concurrency,
                timeout=cfg.timeout_seconds,
                max_retries=cfg.max_retries,
                cache_ttl=cfg.cache_ttl_seconds,
                headers=self.default_headers(),
            )
        return self._client

    async def discover(self, since: datetime | None = None) -> CollectionResult:
        """Birdeye is an enrichment surface, not a primary launch firehose."""
        return CollectionResult(surface=self.name, started_at=utcnow(), finished_at=utcnow())

    async def enrich(self, token: TokenRef, as_of: datetime) -> CollectionResult:
        """Fetch security report and holder distribution for `token`."""
        started = utcnow()
        result = CollectionResult(surface=self.name, started_at=started)

        if not self.api_key:
            # Gracefully degrade if no API key is provided
            result.finished_at = utcnow()
            result.degraded = False
            return result

        mint = token.mint
        try:
            # 1. Fetch token security report
            sec_url = f"/defi/token_security?address={mint}"
            sec_data = await self.client.get_json(sec_url)
            if isinstance(sec_data, dict) and sec_data.get("success"):
                report = self._parse_security(token, sec_data.get("data") or {}, as_of)
                if report:
                    result.security.append(report)

            # 2. Fetch top holders (optional)
            holders_url = f"/defi/v3/token/holder?address={mint}&offset=0&limit=10"
            try:
                holders_data = await self.client.get_json(holders_url)
                if isinstance(holders_data, dict) and holders_data.get("success"):
                    holders = self._parse_holders(token, holders_data.get("data") or {}, as_of)
                    result.holders.extend(holders)
            except Exception as e:
                log.debug("birdeye.holders_failed", mint=mint, error=str(e))

        except HttpError as exc:
            result.ok = False
            result.error = f"HTTP {exc.status}"
            log.warning("birdeye.enrich_http_error", mint=mint, error=str(exc))
        except Exception as exc:
            result.ok = False
            result.error = str(exc)
            log.warning("birdeye.enrich_error", mint=mint, error=str(exc))
        finally:
            result.finished_at = utcnow()

        return result

    def _parse_security(
        self,
        token: TokenRef,
        data: dict[str, Any],
        as_of: datetime,
    ) -> SecurityReport | None:
        """Map Birdeye DeFi security payload onto `SecurityReport`."""
        if not data:
            return None

        # Mint and Freeze authorities: None or null/empty means revoked
        creator = data.get("creatorAddress") or data.get("owner")
        mint_auth = data.get("mintAuthority") or data.get("owner")
        freeze_auth = data.get("freezeAuthority")

        def _is_revoked(val: Any) -> bool:
            if val is None:
                return True
            s = str(val).strip().lower()
            return s in ("", "none", "null", "0", "false")

        mint_revoked = _is_revoked(mint_auth)
        freeze_revoked = _is_revoked(freeze_auth)

        # Top 10 holder percentage
        top10_raw = data.get("top10HolderPercent") or data.get("top10HolderPercentage")
        top10_share = None
        if top10_raw is not None:
            try:
                val = float(top10_raw)
                top10_share = min(1.0, max(0.0, val if val <= 1.0 else val / 100.0))
            except (ValueError, TypeError):
                pass

        # Dev holding percentage
        creator_pct = data.get("creatorPercentage") or data.get("ownerPercentage")
        dev_share = None
        if creator_pct is not None:
            try:
                val = float(creator_pct)
                dev_share = min(1.0, max(0.0, val if val <= 1.0 else val / 100.0))
            except (ValueError, TypeError):
                pass

        # LP burn percentage
        lp_burn = data.get("lpBurnedPercent") or data.get("lpBurnedPercentage")
        lp_share = None
        if lp_burn is not None:
            try:
                val = float(lp_burn)
                lp_share = min(1.0, max(0.0, val if val <= 1.0 else val / 100.0))
            except (ValueError, TypeError):
                pass

        is_mutable = data.get("isMutableMetadata")
        if is_mutable is not None and not isinstance(is_mutable, bool):
            is_mutable = str(is_mutable).lower() == "true"

        return SecurityReport(
            token=token,
            as_of=as_of,
            observed_at=utcnow(),
            mint_authority_revoked=mint_revoked,
            freeze_authority_revoked=freeze_revoked,
            lp_burned_share=lp_share,
            is_mutable_metadata=is_mutable,
            top10_share=top10_share,
            dev_holding_share=dev_share,
            source=f"{self.name}:token_security",
        )

    def _parse_holders(
        self,
        token: TokenRef,
        data: dict[str, Any],
        as_of: datetime,
    ) -> list[HolderRecord]:
        """Parse Birdeye holder accounts."""
        items = data.get("items") or []
        holders = []
        for rank, item in enumerate(items, start=1):
            owner = item.get("owner")
            if not owner:
                continue
            share = item.get("percentage")
            share_float = 0.0
            if share is not None:
                try:
                    v = float(share)
                    share_float = v if v <= 1.0 else v / 100.0
                except (ValueError, TypeError):
                    pass

            holders.append(
                HolderRecord(
                    token=token,
                    as_of=as_of,
                    observed_at=utcnow(),
                    wallet=owner,
                    balance=float(item.get("uiAmount") or 0.0),
                    share_of_supply=min(1.0, max(0.0, share_float)),
                )
            )
        return holders
