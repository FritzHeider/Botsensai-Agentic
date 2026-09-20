"""Dynamic Jito tip floor engine.

Polls Jito's live tip floor API and dynamically calculates bundle tips
calibrated against validator contention, percentile distributions, and trade value.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from botsensai.config import Settings, get_settings
from botsensai.util.logging import get_logger

log = get_logger(__name__)

JITO_TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"

# Known Jito Tip Accounts on Solana Mainnet
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pWHLHzDvf642Wghwq5G8",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]


@dataclass
class TipFloorEstimate:
    p25_lamports: int
    p50_lamports: int
    p75_lamports: int
    p95_lamports: int
    p99_lamports: int
    updated_at: float

    @property
    def is_fresh(self) -> bool:
        return (time.monotonic() - self.updated_at) < 10.0


class JitoTipEngine:
    """Singleton engine managing dynamic tip calculations for MEV bundle landing."""

    _instance: JitoTipEngine | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._floor: TipFloorEstimate = TipFloorEstimate(
            p25_lamports=100_000,
            p50_lamports=500_000,
            p75_lamports=1_500_000,
            p95_lamports=5_000_000,
            p99_lamports=15_000_000,
            updated_at=time.monotonic(),
        )
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> JitoTipEngine:
        if cls._instance is None:
            cls._instance = cls(settings)
        return cls._instance

    @property
    def current_floor(self) -> TipFloorEstimate:
        return self._floor

    async def fetch_tip_floor(self) -> TipFloorEstimate:
        """Query Jito public tip floor API."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(JITO_TIP_FLOOR_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    # Tip floor data returns array of objects with percentile fields
                    if isinstance(data, list) and data:
                        entry = data[0]
                        # Tip floors in SOL or lamports
                        p25 = int(entry.get("landed_tips_25th_percentile", 0.0001) * 1e9)
                        p50 = int(entry.get("landed_tips_50th_percentile", 0.0005) * 1e9)
                        p75 = int(entry.get("landed_tips_75th_percentile", 0.0015) * 1e9)
                        p95 = int(entry.get("landed_tips_95th_percentile", 0.0050) * 1e9)
                        p99 = int(entry.get("landed_tips_99th_percentile", 0.0150) * 1e9)
                        self._floor = TipFloorEstimate(
                            p25_lamports=max(10_000, p25),
                            p50_lamports=max(50_000, p50),
                            p75_lamports=max(100_000, p75),
                            p95_lamports=max(500_000, p95),
                            p99_lamports=max(1_000_000, p99),
                            updated_at=time.monotonic(),
                        )
                        return self._floor
        except Exception as err:
            log.debug("jito_tips.fetch_failed", error=str(err))
        return self._floor

    def calculate_tip_lamports(
        self,
        conviction_score: float = 0.70,
        trade_size_sol: float = 0.10,
        is_contended: bool = False,
    ) -> int:
        """Calculate optimal tip in lamports balancing landing rate against edge erosion."""
        if not self.settings.execution.dynamic_jito_tips:
            return self.settings.execution.jito_tip_lamports

        floor = self._floor
        # High conviction + contended mints target the 75th percentile to win slot-0
        if conviction_score >= 0.82 or is_contended:
            target_lamports = floor.p75_lamports
        elif conviction_score >= 0.72:
            target_lamports = floor.p50_lamports
        else:
            target_lamports = floor.p25_lamports

        # Cap tip at max 5% of trade size to protect expectancy
        max_allowed_tip = int(trade_size_sol * 1e9 * 0.05)
        # Floor at minimum 50,000 lamports (0.00005 SOL)
        tip = max(50_000, min(target_lamports, max(100_000, max_allowed_tip)))
        return tip

    async def start_poller(self, interval_seconds: float = 5.0) -> None:
        if self._running:
            return
        self._running = True

        async def _loop() -> None:
            while self._running:
                try:
                    await self.fetch_tip_floor()
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass
                await asyncio.sleep(interval_seconds)

        self._task = asyncio.create_task(_loop())

    def stop_poller(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
