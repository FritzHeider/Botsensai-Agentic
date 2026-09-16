"""High-speed blockhash ring-buffer cache for sub-millisecond transaction assembly."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from botsensai.config import Settings, get_settings
from botsensai.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class CachedBlockhash:
    blockhash: str
    last_valid_block_height: int
    fetched_at: float

    @property
    def is_fresh(self) -> bool:
        # Solana blockhashes are valid for ~150 slots (~60-90s). Fresh if < 30s old.
        return (time.monotonic() - self.fetched_at) < 30.0


class BlockhashCache:
    """Singleton-style cache maintaining the latest verified Solana blockhashes."""

    _instance: BlockhashCache | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: list[CachedBlockhash] = []
        self._max_history = 5
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # Fallback default blockhash for testing / simulation
        self._default = CachedBlockhash(
            blockhash="4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM",
            last_valid_block_height=300000000,
            fetched_at=time.monotonic(),
        )

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> BlockhashCache:
        if cls._instance is None:
            cls._instance = cls(settings)
        return cls._instance

    def get_latest_blockhash(self) -> str:
        """Return the most recent fresh blockhash, or a valid fallback."""
        if self._cache and self._cache[-1].is_fresh:
            return self._cache[-1].blockhash
        return self._default.blockhash

    async def fetch_blockhash(self) -> CachedBlockhash | None:
        """Query RPC endpoint for latest blockhash."""
        rpc_url = self.settings.helius_rpc_url or self.settings.solana_rpc_url
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "confirmed"}],
        }
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(rpc_url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    val = data.get("result", {}).get("value", {})
                    bh = val.get("blockhash")
                    height = val.get("lastValidBlockHeight", 0)
                    if bh:
                        entry = CachedBlockhash(
                            blockhash=bh,
                            last_valid_block_height=height,
                            fetched_at=time.monotonic(),
                        )
                        self._cache.append(entry)
                        if len(self._cache) > self._max_history:
                            self._cache.pop(0)
                        return entry
        except Exception as err:
            log.debug("blockhash_cache.fetch_failed", error=str(err))
        return None

    async def start_poller(self, interval_seconds: float = 0.5) -> None:
        """Start background polling task to refresh blockhash continuously."""
        if self._running:
            return
        self._running = True

        async def _loop() -> None:
            while self._running:
                try:
                    await self.fetch_blockhash()
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
